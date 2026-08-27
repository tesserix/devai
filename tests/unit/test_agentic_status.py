from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

import devai.adapters.llm as llm_module
import devai.agentic.kagent_client as kagent_client_module
import devai.agentic.routes as routes
import devai.agentic.status as status_module
from devai.adapters.llm import LLMResponse
from devai.adapters.llm.base import LLMUsage
from devai.agentic.status import AgenticStatus, ComponentStatus, fetch_agentic_status


def _component(name: str) -> ComponentStatus:
    return ComponentStatus(
        name=name,
        role="controller",
        namespace="test",
        url="http://test.test.svc.cluster.local",
        reachable=True,
    )


def _snapshot() -> AgenticStatus:
    return AgenticStatus(
        registry=_component("registry"),
        agentgateway=_component("agentgateway"),
        ai_gateway=_component("ai-gateway"),
        kagent=_component("kagent"),
    )


def test_status_uses_controller_and_llm_gateway_urls(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}

    def fake_fetch(**kwargs: Any) -> AgenticStatus:
        captured.update(kwargs)
        return _snapshot()

    async def fake_sandbox(_: Any) -> ComponentStatus:
        return _component("sandboxes")

    monkeypatch.setattr(routes, "fetch_agentic_status", fake_fetch)
    monkeypatch.setattr(routes, "probe_sandbox_storage", fake_sandbox)
    app = FastAPI()
    app.state.config = SimpleNamespace(
        require_auth=False,
        agentgateway_url="http://agentgateway-mcp.agentgateway-system.svc.cluster.local:8080",
        agentgateway_controller_url="http://agentgateway.agentgateway-system.svc.cluster.local:9092",
        ai_gateway_url="",
        llm_gateway_base_url="http://ai-gateway.agentgateway-system.svc.cluster.local:8080",
        kagent_url="http://kagent-controller.kagent-system.svc.cluster.local:8083",
    )
    app.state.registry_client = None
    app.state.sandbox_service = None
    app.include_router(routes.router)

    response = TestClient(app).get("/api/agentic/status")

    assert response.status_code == 200
    assert captured["agentgateway_url"] == ("http://agentgateway.agentgateway-system.svc.cluster.local:9092")
    assert captured["ai_gateway_url"] == ("http://ai-gateway.agentgateway-system.svc.cluster.local:8080")


def test_ai_gateway_is_reachable_when_listener_returns_404(monkeypatch: Any) -> None:
    requested: list[str] = []

    class Response:
        def __init__(self, status_code: int, text: str = "") -> None:
            self.status_code = status_code
            self.text = text

    def fake_get(url: str, *, timeout: float) -> Response:
        requested.append(url)
        if url.endswith("/metrics") or url.endswith("/version"):
            return Response(200, "ok")
        return Response(404, "route not found")

    monkeypatch.setattr(status_module, "_probe_url_allowed", lambda _: (True, ""))
    monkeypatch.setattr(httpx, "get", fake_get)

    snapshot = fetch_agentic_status(
        registry_client=None,
        agentgateway_url="http://agentgateway.agentgateway-system.svc.cluster.local:9092",
        ai_gateway_url="http://ai-gateway.agentgateway-system.svc.cluster.local:8080",
        kagent_url="http://kagent-controller.kagent-system.svc.cluster.local:8083",
    )

    assert snapshot.agentgateway.reachable is True
    assert snapshot.ai_gateway.reachable is True
    assert "http://ai-gateway.agentgateway-system.svc.cluster.local:8080/" in requested


def test_llm_probe_uses_runtime_fallback_chain_and_normalized_usage(monkeypatch: Any) -> None:
    runtime_config = SimpleNamespace(require_auth=False)
    captured: dict[str, Any] = {}

    class Adapter:
        provider_name = "vertex_gemini→anthropic"

        async def generate(self, _: Any) -> LLMResponse:
            return LLMResponse(
                text="Gateway healthy",
                usage=LLMUsage(prompt_tokens=7, completion_tokens=2, total_tokens=9),
                finish_reason="stop",
                model="claude-sonnet-4-6",
                provider="anthropic",
            )

    def fake_create_llm_chain(config: Any) -> Adapter:
        captured["config"] = config
        return Adapter()

    monkeypatch.setattr(llm_module, "create_llm_chain", fake_create_llm_chain)
    app = FastAPI()
    app.state.config = runtime_config
    app.include_router(routes.router)

    response = TestClient(app).get("/api/agentic/llm-probe")

    assert response.status_code == 200
    assert captured["config"] is runtime_config
    assert response.json() == {
        "ok": True,
        "adapter": "Adapter",
        "provider": "vertex_gemini→anthropic",
        "model": "claude-sonnet-4-6",
        "text": "Gateway healthy",
        "usage": {"input": 7, "output": 2},
    }


def test_llm_probe_reports_adapter_error_response_as_failure(monkeypatch: Any) -> None:
    class Adapter:
        provider_name = "vertex_gemini"

        async def generate(self, _: Any) -> LLMResponse:
            return LLMResponse(
                text="backend authentication failed",
                finish_reason="error",
                model="gemini-2.5-flash",
                provider="vertex_gemini",
            )

    monkeypatch.setattr(llm_module, "create_llm_chain", lambda _: Adapter())
    app = FastAPI()
    app.state.config = SimpleNamespace(require_auth=False)
    app.include_router(routes.router)

    response = TestClient(app).get("/api/agentic/llm-probe")

    assert response.status_code == 200
    assert response.json() == {
        "ok": False,
        "adapter": "Adapter",
        "provider": "vertex_gemini",
        "model": "gemini-2.5-flash",
        "error": "backend authentication failed",
    }


def test_llm_probe_rejects_empty_completion_with_sufficient_budget(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}

    class Adapter:
        provider_name = "anthropic"

        async def generate(self, request: Any) -> LLMResponse:
            captured["max_tokens"] = request.max_tokens
            return LLMResponse(
                text="",
                finish_reason="max_tokens",
                model="claude-sonnet-4-6",
                provider="anthropic",
            )

    monkeypatch.setattr(llm_module, "create_llm_chain", lambda _: Adapter())
    app = FastAPI()
    app.state.config = SimpleNamespace(require_auth=False)
    app.include_router(routes.router)

    response = TestClient(app).get("/api/agentic/llm-probe")

    assert response.status_code == 200
    assert captured["max_tokens"] == 128
    assert response.json() == {
        "ok": False,
        "adapter": "Adapter",
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "error": "model returned no text (finish_reason=max_tokens)",
    }


def test_kagent_dispatch_uses_request_scoped_a2a_ids(monkeypatch: Any) -> None:
    calls: list[dict[str, Any]] = []

    class Client:
        async def dispatch(self, agent: str, message: str, **kwargs: Any) -> dict[str, Any]:
            calls.append({"agent": agent, "message": message, **kwargs})
            return {"status": {"state": "completed"}}

    monkeypatch.setattr(kagent_client_module, "create_kagent_client", lambda _: Client())
    app = FastAPI()
    app.state.config = SimpleNamespace(require_auth=False)
    app.include_router(routes.router)

    response = TestClient(app).post(
        "/api/agentic/kagent/reviewer/dispatch",
        headers={"x-request-id": "request-123"},
        json={"message": "review this", "namespace": "kagent-system"},
    )

    assert response.status_code == 200
    assert calls[0]["request_id"] == "request-123:kagent:kagent-system:reviewer"
    assert calls[0]["message_id"] == "request-123:kagent:kagent-system:reviewer"


def test_kagent_dispatch_surfaces_uncertain_outcome_without_remote_detail(monkeypatch: Any) -> None:
    class Client:
        async def dispatch(self, agent: str, message: str, **kwargs: Any) -> dict[str, Any]:
            raise kagent_client_module.KagentDispatchOutcomeUncertain("secret remote body")

    monkeypatch.setattr(kagent_client_module, "create_kagent_client", lambda _: Client())
    app = FastAPI()
    app.state.config = SimpleNamespace(require_auth=False)
    app.include_router(routes.router)

    response = TestClient(app).post(
        "/api/agentic/kagent/reviewer/dispatch",
        headers={"x-request-id": "request-123"},
        json={"message": "review this", "namespace": "kagent-system"},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "kagent_dispatch_outcome_uncertain"
    assert "secret remote body" not in response.text
