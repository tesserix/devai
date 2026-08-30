from __future__ import annotations

import hashlib
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from devai.a2a.routes import router
from devai.adapters.llm.base import LLMResponse
from devai.config import Settings
from devai.pipeline.interfaces import StageDeps
from devai.registry.client import Agent, ResolvedAgent
from devai.specializations.loader import load_specialization_from_string
from devai.specializations.registry import SpecializationRegistry
from devai.specializations.service import SpecializationService

_RUNTIME_TOKEN = "global-adk-runtime-upstream-token-123"


class ScriptedGatewayLLM:
    provider_name = "gateway"

    async def generate(self, request):  # noqa: ANN001
        return LLMResponse(text='{"summary":"A2A completed"}')


class PipelineService:
    def __init__(self, deps: StageDeps) -> None:
        self.stage_deps = deps


class ResolvedCatalog:
    def __init__(self, specs: SpecializationRegistry) -> None:
        self.specs = specs

    def resolve_agent(self, name: str) -> ResolvedAgent:
        capability = name.removesuffix("-agent").replace("-", "_")
        spec = self.specs.resolve(capability)
        ref = capability.replace("_", "-")
        prompt_name = f"{ref}-prompt-v1"
        return ResolvedAgent(
            agent=Agent(
                name=name,
                description=spec.description,
                version="1.0.0",
                model_provider="devai-user-routing",
                model_name="dynamic",
                skills=[ref],
                prompts=[prompt_name],
                labels={
                    "devai.io/source": "devai",
                    "devai.io/risk-level": spec.risk_level.value,
                    "ai.tesserix.dev/runtime": "tesserix-adk",
                    "ai.tesserix.dev/provider-policy": "user-connectors",
                },
            ),
            resolved={
                "skills": [
                    {
                        "kind": "Skill",
                        "metadata": {
                            "name": ref,
                            "labels": {"devai.io/risk-level": spec.risk_level.value},
                        },
                        "spec": {
                            "category": spec.category,
                            "tools": list(spec.allowed_tools),
                            "contextKeys": list(spec.context_keys),
                            "outputKey": spec.output_key,
                            "handoverSchema": {
                                key: {
                                    "type": field.type,
                                    "required": field.required,
                                    "description": field.description,
                                }
                                for key, field in spec.handover_schema.items()
                            },
                        },
                    }
                ],
                "prompts": [
                    {
                        "kind": "Prompt",
                        "metadata": {
                            "name": prompt_name,
                            "labels": {
                                "devai.io/prompt-hash": hashlib.sha256(spec.system_prompt.encode("utf-8")).hexdigest()[
                                    :12
                                ]
                            },
                        },
                        "spec": {
                            "systemPrompt": spec.system_prompt,
                            "userPromptTemplate": spec.user_prompt_template,
                        },
                    }
                ],
            },
            unresolved=[],
        )


def _client(*, governed: bool = True, llm_available: bool = True) -> TestClient:
    config = Settings(
        adk_runtime_service_token=_RUNTIME_TOKEN,
        auth_bff_shared_secret="test-shared-secret",
        llm_gateway_required=True,
        llm_gateway_base_url="http://ai-gateway:8080",
    )
    registry = SpecializationRegistry()
    registry.register(
        load_specialization_from_string(
            """
name: requirements_analyst
runtime: tesserix_adk
system_prompt: Analyze requirements.
handover_schema:
  summary: string
"""
        )
    )
    registry.register(
        load_specialization_from_string(
            """
name: release_manager
runtime: tesserix_adk
risk_level: critical
system_prompt: Release approved changes.
handover_schema:
  summary: string
"""
        )
    )
    service = SpecializationService(
        config,
        registry_client=ResolvedCatalog(registry) if governed else None,
    )
    service._registry = registry
    service._reviewed_capabilities = frozenset(spec.name for spec in registry.all())
    service._started = True
    app = FastAPI()
    app.state.config = config
    app.state.specialization_service = service
    app.state.pipeline_service = PipelineService(
        StageDeps(config=config, llm=ScriptedGatewayLLM() if llm_available else None)
    )
    app.include_router(router)
    return TestClient(app)


def _request() -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": "request-1",
        "method": "message/send",
        "params": {
            "message": {
                "role": "user",
                "parts": [{"kind": "text", "text": "Analyze a small login feature"}],
            }
        },
    }


def _runtime_headers(token: str = _RUNTIME_TOKEN) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "X-ADK-Workload-Subject": "zitadel-service-user-123",
        "X-ADK-Workload-Client-ID": "inventory-agent",
    }


def test_a2a_endpoint_requires_a_verified_principal() -> None:
    response = _client().post("/a2a/v1/requirements-analyst", json=_request())

    assert response.status_code == 401


def test_a2a_endpoint_accepts_agentgateway_runtime_identity() -> None:
    response = _client().post(
        "/a2a/v1/requirements-analyst-agent",
        json=_request(),
        headers=_runtime_headers(),
    )

    assert response.status_code == 200


def test_a2a_endpoint_rejects_spoofed_runtime_identity_without_the_upstream_token() -> None:
    headers = _runtime_headers()
    headers.pop("Authorization")

    response = _client().post(
        "/a2a/v1/requirements-analyst-agent",
        json=_request(),
        headers=headers,
    )

    assert response.status_code == 401


def test_a2a_endpoint_rejects_runtime_identity_with_the_wrong_upstream_token() -> None:
    response = _client().post(
        "/a2a/v1/requirements-analyst-agent",
        json=_request(),
        headers=_runtime_headers("wrong-global-runtime-token-123"),
    )

    assert response.status_code == 401


def test_a2a_endpoint_requires_auditable_runtime_identity_headers() -> None:
    response = _client().post(
        "/a2a/v1/requirements-analyst-agent",
        json=_request(),
        headers={"Authorization": f"Bearer {_RUNTIME_TOKEN}"},
    )

    assert response.status_code == 401


def test_a2a_endpoint_runs_catalog_agent_with_forwarded_identity() -> None:
    response = _client().post(
        "/a2a/v1/requirements-analyst-agent",
        json=_request(),
        headers={
            "X-Forwarded-User": "owner@example.com",
            "X-Forwarded-Uid": "owner-1",
            "X-Forwarded-Tenant": "tenant-a",
            "X-Auth-Bff-Secret": "test-shared-secret",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == "request-1"
    assert body["result"]["status"] == {"state": "completed"}
    assert body["result"]["artifacts"][0]["parts"][0]["kind"] == "text"
    assert '"summary": "A2A completed"' in body["result"]["artifacts"][0]["parts"][0]["text"]


def test_capability_endpoint_selects_agent_and_returns_registry_bundle_snapshot() -> None:
    response = _client().post(
        "/a2a/v1/capabilities/requirements-analyst",
        json=_request(),
        headers={
            "X-Forwarded-User": "owner@example.com",
            "X-Forwarded-Uid": "owner-1",
            "X-Forwarded-Tenant": "tenant-a",
            "X-Auth-Bff-Secret": "test-shared-secret",
        },
    )

    assert response.status_code == 200
    governance = response.json()["result"]["metadata"]["governance"]
    assert governance == {
        "capability": "requirements_analyst",
        "agent": "requirements-analyst-agent",
        "version": "1.0.0",
        "skills": ["requirements-analyst"],
        "tools": [],
        "mcpServers": [],
        "prompts": ["requirements-analyst-prompt-v1"],
    }


def test_a2a_endpoint_returns_404_for_unknown_agent_without_revealing_catalog() -> None:
    response = _client().post(
        "/a2a/v1/missing",
        json=_request(),
        headers={
            "X-Forwarded-User": "owner@example.com",
            "X-Auth-Bff-Secret": "test-shared-secret",
        },
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "agent not found"}


def test_a2a_endpoint_rejects_messages_over_the_aggregate_limit() -> None:
    body = _request()
    body["params"]["message"]["parts"] = [{"kind": "text", "text": "x" * 65_536} for _ in range(5)]

    response = _client().post(
        "/a2a/v1/requirements-analyst",
        json=body,
        headers={
            "X-Forwarded-User": "owner@example.com",
            "X-Auth-Bff-Secret": "test-shared-secret",
        },
    )

    assert response.status_code == 422


def test_a2a_endpoint_rejects_an_oversized_jsonrpc_id() -> None:
    body = _request()
    body["id"] = "x" * 129

    response = _client().post(
        "/a2a/v1/requirements-analyst",
        json=body,
        headers={
            "X-Forwarded-User": "owner@example.com",
            "X-Auth-Bff-Secret": "test-shared-secret",
        },
    )

    assert response.status_code == 422


def test_a2a_endpoint_does_not_bypass_high_risk_workflow_approval() -> None:
    response = _client().post(
        "/a2a/v1/release-manager",
        json=_request(),
        headers={
            "X-Forwarded-User": "owner@example.com",
            "X-Auth-Bff-Secret": "test-shared-secret",
        },
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "agent requires workflow approval"}


def test_named_a2a_endpoint_cannot_bypass_registry_admission() -> None:
    response = _client(governed=False).post(
        "/a2a/v1/requirements-analyst-agent",
        json=_request(),
        headers={
            "X-Forwarded-User": "owner@example.com",
            "X-Auth-Bff-Secret": "test-shared-secret",
        },
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "agent composition unavailable"}


def test_governed_a2a_fails_closed_when_no_model_adapter_is_available() -> None:
    response = _client(llm_available=False).post(
        "/a2a/v1/capabilities/requirements-analyst",
        json=_request(),
        headers={
            "X-Forwarded-User": "owner@example.com",
            "X-Auth-Bff-Secret": "test-shared-secret",
        },
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "agent execution unavailable"}
