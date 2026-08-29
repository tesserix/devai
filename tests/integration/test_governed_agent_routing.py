from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from devai.a2a.routes import router
from devai.adapters.llm.base import LLMResponse
from devai.config import Settings
from devai.pipeline.interfaces import StageDeps
from devai.registry.client import RegistryClient
from devai.specializations.service import SpecializationService

_PROMPT = "Return a concise governed result."


class _ScriptedGateway:
    provider_name = "gateway"

    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, request):  # noqa: ANN001
        self.calls += 1
        return LLMResponse(text='{"summary":"governed end to end"}')


class _PipelineService:
    def __init__(self, deps: StageDeps) -> None:
        self.stage_deps = deps


def _resolution() -> dict[str, Any]:
    return {
        "agent": {
            "kind": "Agent",
            "metadata": {
                "name": "reviewer-agent",
                "tag": "1.0.0",
                "labels": {
                    "devai.io/source": "devai",
                    "devai.io/risk-level": "low",
                    "ai.tesserix.dev/runtime": "tesserix-adk",
                    "ai.tesserix.dev/provider-policy": "user-connectors",
                },
            },
            "spec": {
                "model": {"provider": "devai-user-routing", "name": "dynamic"},
                "skills": ["reviewer"],
                "prompts": ["reviewer-prompt-v1"],
            },
        },
        "resolved": {
            "skills": [
                {
                    "kind": "Skill",
                    "metadata": {
                        "name": "reviewer",
                        "labels": {"devai.io/risk-level": "low"},
                    },
                    "spec": {
                        "category": "review",
                        "tools": [],
                        "contextKeys": [],
                        "outputKey": "reviewer_output",
                        "handoverSchema": {
                            "summary": {
                                "type": "string",
                                "required": True,
                            }
                        },
                    },
                }
            ],
            "prompts": [
                {
                    "kind": "Prompt",
                    "metadata": {
                        "name": "reviewer-prompt-v1",
                        "labels": {"devai.io/prompt-hash": hashlib.sha256(_PROMPT.encode("utf-8")).hexdigest()[:12]},
                    },
                    "spec": {"systemPrompt": _PROMPT, "userPromptTemplate": ""},
                }
            ],
        },
        "unresolved": [],
    }


async def test_registry_capability_adk_and_gateway_complete_one_governed_loop(
    tmp_path: Path,
    monkeypatch,
) -> None:
    (tmp_path / "reviewer.yaml").write_text(
        "\n".join(
            [
                "name: reviewer",
                "display_name: Reviewer",
                "description: Reviews work.",
                "category: review",
                "runtime: tesserix_adk",
                "risk_level: low",
                "system_prompt: Return a concise governed result.",
                "handover_schema:",
                "  summary:",
                "    type: string",
                "    required: true",
            ]
        )
    )
    requested: list[str] = []

    def registry_request(method: str, url: str, **_: object) -> httpx.Response:
        assert method == "GET"
        requested.append(url)
        return httpx.Response(200, json=_resolution())

    monkeypatch.setattr(httpx, "request", registry_request)
    config = Settings(
        auth_bff_shared_secret="test-shared-secret",
        llm_gateway_required=True,
        llm_gateway_base_url="http://ai-gateway:8080",
    )
    registry = RegistryClient(
        base_url="http://agentic-registry:12121",
        namespace="devai",
    )
    specializations = SpecializationService(
        config,
        directory=tmp_path,
        registry_client=registry,
    )
    await specializations.start()
    gateway = _ScriptedGateway()
    app = FastAPI()
    app.state.specialization_service = specializations
    app.state.pipeline_service = _PipelineService(StageDeps(config=config, llm=gateway))
    app.include_router(router)

    with TestClient(app) as client:
        response = client.post(
            "/a2a/v1/capabilities/reviewer",
            headers={
                "X-Forwarded-User": "reviewer@example.com",
                "X-Forwarded-Tenant": "tenant-a",
                "X-Auth-Bff-Secret": "test-shared-secret",
            },
            json={
                "jsonrpc": "2.0",
                "id": "e2e-1",
                "method": "message/send",
                "params": {
                    "message": {
                        "role": "user",
                        "parts": [{"kind": "text", "text": "Review this change"}],
                    }
                },
            },
        )

    assert response.status_code == 200, response.text
    assert gateway.calls == 1
    assert requested == ["http://agentic-registry:12121/v0/agents/reviewer-agent/resolved?namespace=devai"]
    governance = response.json()["result"]["metadata"]["governance"]
    assert governance["agent"] == "reviewer-agent"
    assert governance["skills"] == ["reviewer"]
    assert governance["prompts"] == ["reviewer-prompt-v1"]
    assert '"summary": "governed end to end"' in response.json()["result"]["artifacts"][0]["parts"][0]["text"]
