from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from devai.a2a.routes import router
from devai.adapters.llm.base import LLMResponse
from devai.config import Settings
from devai.pipeline.interfaces import StageDeps
from devai.specializations.loader import load_specialization_from_string
from devai.specializations.registry import SpecializationRegistry
from devai.specializations.service import SpecializationService


class ScriptedLLM:
    provider_name = "vertex_gemini"

    async def generate(self, request):  # noqa: ANN001
        return LLMResponse(text='{"summary":"A2A completed"}')


class PipelineService:
    def __init__(self, deps: StageDeps) -> None:
        self.stage_deps = deps


def _client() -> TestClient:
    config = Settings(auth_bff_shared_secret="test-shared-secret")
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
    service = SpecializationService(config)
    service._registry = registry
    service._started = True
    app = FastAPI()
    app.state.config = config
    app.state.specialization_service = service
    app.state.pipeline_service = PipelineService(StageDeps(config=config, llm=ScriptedLLM()))
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


def test_a2a_endpoint_requires_a_verified_principal() -> None:
    response = _client().post("/a2a/v1/requirements-analyst", json=_request())

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
