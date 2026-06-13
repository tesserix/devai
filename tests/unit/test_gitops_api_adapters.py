"""Argo CD REST + Kargo Connect-RPC API-mode adapters (external instances).

Mocks the HTTP layer so the tests need no network: proves the adapters speak
the right endpoints/messages, parse responses into the family shape, and honor
the mutation gate.
"""

from __future__ import annotations

from typing import Any

import pytest

from devai.adapters.gitops.argocd_api import ArgoCDApiAdapter
from devai.adapters.gitops.kargo_api import KargoApiAdapter


class _Resp:
    def __init__(self, payload: Any):
        self._payload = payload
        self.content = b"x"

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self._payload


class _Client:
    """Records requests; returns canned payloads keyed by URL suffix."""

    def __init__(self, routes: dict[str, Any], sink: list[tuple[str, str, Any]]):
        self._routes = routes
        self._sink = sink

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def request(self, method: str, url: str, **kw):
        self._sink.append((method, url, kw.get("json")))
        for suffix, payload in self._routes.items():
            if url.endswith(suffix):
                return _Resp(payload)
        return _Resp({})

    async def post(self, url: str, **kw):
        return await self.request("POST", url, **kw)


@pytest.fixture
def httpx_mock(monkeypatch):
    sink: list[tuple[str, str, Any]] = []
    routes: dict[str, Any] = {}

    import httpx

    def fake_async_client(*a, **k):
        return _Client(routes, sink)

    monkeypatch.setattr(httpx, "AsyncClient", fake_async_client)
    return routes, sink


# ── Argo CD REST ───────────────────────────────────────────────────────────


async def test_argocd_api_list_and_get(httpx_mock):
    routes, _ = httpx_mock
    routes["/api/v1/applications"] = {
        "items": [
            {
                "metadata": {"name": "web"},
                "spec": {"project": "default", "destination": {"namespace": "apps"}},
                "status": {"sync": {"status": "Synced"}, "health": {"status": "Healthy"}},
            }
        ]
    }
    a = ArgoCDApiAdapter("https://argo.example.com", "tok")
    apps = await a.list_targets()
    assert apps[0]["name"] == "web" and apps[0]["health_status"] == "Healthy"


async def test_argocd_api_sync_posts(httpx_mock):
    routes, sink = httpx_mock
    a = ArgoCDApiAdapter("https://argo.example.com", "tok")
    res = await a.sync("web")
    assert res["ok"] is True
    assert any(m == "POST" and u.endswith("/api/v1/applications/web/sync") for m, u, _ in sink)


async def test_argocd_api_mutation_gate():
    a = ArgoCDApiAdapter("https://argo.example.com", "tok", mutations_enabled=False)
    res = await a.sync("web")
    assert res["ok"] is False and "disabled" in res["error"]


# ── Kargo Connect-RPC ──────────────────────────────────────────────────────


async def test_kargo_api_list_stages(httpx_mock):
    routes, sink = httpx_mock
    routes["/ListStages"] = {
        "stages": [{"metadata": {"name": "prod"}, "status": {"phase": "Steady", "health": {"status": "Healthy"}}}]
    }
    k = KargoApiAdapter("https://kargo.example.com", "tok", default_project="demo")
    stages = await k.list_targets()
    assert stages[0]["stage"] == "prod" and stages[0]["health"] == "Healthy"
    # Connect-RPC posts the project in the message body.
    assert sink[0][2] == {"project": "demo"}


async def test_kargo_api_promote_posts_message(httpx_mock):
    routes, sink = httpx_mock
    routes["/PromoteToStage"] = {"promotion": {"metadata": {"name": "prod-abc12"}}}
    k = KargoApiAdapter("https://kargo.example.com", "tok")
    res = await k.promote("demo", "prod", "fr-1")
    assert res["ok"] is True and res["promotion"] == "prod-abc12"
    assert sink[0][2] == {"project": "demo", "stage": "prod", "freight": "fr-1"}


async def test_kargo_api_mutation_gate():
    k = KargoApiAdapter("https://kargo.example.com", "tok", mutations_enabled=False)
    res = await k.promote("demo", "prod", "fr-1")
    assert res["ok"] is False and "disabled" in res["error"]
