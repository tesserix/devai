from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

import devai.agentic.routes as routes
import devai.agentic.status as status_module
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
