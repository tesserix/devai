"""HTTP layer for sandboxes (#179) — /api/sandboxes/*."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from devai.sandbox.routes import router
from devai.sandbox.service import SandboxService
from tests.unit.test_sandbox import _FakeDB

_SAM = {"X-Forwarded-Email": "sam@example.com"}
_MALLORY = {"X-Forwarded-Email": "mallory@example.com"}
_SPEC: dict[str, Any] = {
    "agent": {"name": "code-remediator-agent", "version": "v1.8.2"},
    "model": {"provider": "anthropic", "model": "claude-sonnet-4-20250514"},
}


def _client(*, wired: bool = True) -> TestClient:
    app = FastAPI()
    app.state.sandbox_service = SandboxService(_FakeDB()) if wired else None
    app.include_router(router)
    return TestClient(app)


def test_create_returns_the_pinned_spec() -> None:
    r = _client().post("/api/sandboxes", json=_SPEC, headers=_SAM)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "pending"
    assert body["spec"]["tools"]["default_mode"] == "mock"
    assert body["expires_at"] > body["created_at"]


def test_invalid_spec_is_rejected() -> None:
    r = _client().post("/api/sandboxes", json={"agent": {"name": "a", "version": "v1"}}, headers=_SAM)
    assert r.status_code == 422


def test_another_owner_cannot_read_or_destroy() -> None:
    client = _client()
    sid = client.post("/api/sandboxes", json=_SPEC, headers=_SAM).json()["id"]

    assert client.get(f"/api/sandboxes/{sid}", headers=_MALLORY).status_code == 404
    assert client.delete(f"/api/sandboxes/{sid}", headers=_MALLORY).status_code == 404
    assert client.get(f"/api/sandboxes/{sid}", headers=_SAM).status_code == 200


def test_list_is_owner_scoped() -> None:
    client = _client()
    client.post("/api/sandboxes", json=_SPEC, headers=_SAM)
    client.post("/api/sandboxes", json=_SPEC, headers=_MALLORY)

    assert len(client.get("/api/sandboxes", headers=_SAM).json()) == 1


def test_destroy_is_terminal() -> None:
    client = _client()
    sid = client.post("/api/sandboxes", json=_SPEC, headers=_SAM).json()["id"]

    assert client.delete(f"/api/sandboxes/{sid}", headers=_SAM).status_code == 200
    assert client.get(f"/api/sandboxes/{sid}", headers=_SAM).json()["status"] == "destroyed"


def test_routes_503_until_the_service_is_wired() -> None:
    assert _client(wired=False).post("/api/sandboxes", json=_SPEC, headers=_SAM).status_code == 503
