"""HTTP layer for sandboxes (#179) — /api/sandboxes/*."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from devai.identity import Principal
from devai.sandbox.routes import _is_admin, router
from devai.sandbox.service import SandboxService
from tests.unit.test_sandbox import _FakeDB

_SAM = {"X-Forwarded-Email": "sam@example.com"}
_MALLORY = {"X-Forwarded-Email": "mallory@example.com"}
_TENANT_A = {
    "X-Forwarded-Email": "same@example.com",
    "X-Forwarded-Uid": "same-subject",
    "X-Forwarded-Tenant": "tenant-a",
}
_TENANT_B = {**_TENANT_A, "X-Forwarded-Tenant": "tenant-b"}
_SPEC: dict[str, Any] = {
    "agent": {"name": "code-remediator-agent", "version": "v1.8.2"},
    "model": {"provider": "anthropic", "model": "claude-sonnet-4-20250514"},
}


def _client(*, wired: bool = True, db: Any | None = None) -> TestClient:
    app = FastAPI()
    app.state.sandbox_service = SandboxService(db or _FakeDB()) if wired else None
    app.include_router(router)
    return TestClient(app)


def test_create_returns_the_pinned_spec() -> None:
    r = _client().post("/api/sandboxes", json=_SPEC, headers=_SAM)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "pending"
    assert body["spec"]["tools"]["default_mode"] == "mock"
    assert body["expires_at"] > body["created_at"]


def test_create_derives_quota_identity_from_the_authenticated_tenant() -> None:
    db = _FakeDB()
    response = _client(db=db).post("/api/sandboxes", json=_SPEC, headers=_TENANT_A)

    assert response.status_code == 201
    row = next(iter(db.rows.values()))
    assert row["tenant_id"] == "tenant-a"
    assert row["user_id"] == "same-subject"


def test_invalid_spec_is_rejected() -> None:
    r = _client().post("/api/sandboxes", json={"agent": {"name": "a", "version": "v1"}}, headers=_SAM)
    assert r.status_code == 422


def test_another_owner_cannot_read_or_destroy() -> None:
    client = _client()
    sid = client.post("/api/sandboxes", json=_SPEC, headers=_SAM).json()["id"]

    assert client.get(f"/api/sandboxes/{sid}", headers=_MALLORY).status_code == 404
    assert client.delete(f"/api/sandboxes/{sid}", headers=_MALLORY).status_code == 404
    assert client.get(f"/api/sandboxes/{sid}", headers=_SAM).status_code == 200


def test_same_email_in_another_tenant_cannot_read_or_destroy() -> None:
    client = _client()
    sid = client.post("/api/sandboxes", json=_SPEC, headers=_TENANT_A).json()["id"]

    assert client.get(f"/api/sandboxes/{sid}", headers=_TENANT_B).status_code == 404
    assert client.delete(f"/api/sandboxes/{sid}", headers=_TENANT_B).status_code == 404
    assert client.get(f"/api/sandboxes/{sid}", headers=_TENANT_A).status_code == 200


def test_only_explicit_platform_admin_has_cross_owner_access() -> None:
    assert _is_admin(Principal(email="admin@example.com", tenant_id="tenant-b", roles=["admin"])) is False
    assert _is_admin(Principal(email="legacy-platform@example.com", roles=["admin"])) is True
    assert _is_admin(Principal(email="platform@example.com", roles=["platform-admin"])) is True


def test_platform_admin_session_can_read_across_owners() -> None:
    class _Redis:
        async def get(self, key: str) -> str:
            assert key == "devai:session:platform-session"
            return json.dumps(
                {
                    "user_email": "platform@example.com",
                    "user_login": "platform-admin",
                    "roles": ["platform-admin"],
                }
            )

    client = _client()
    client.app.state.state_manager = SimpleNamespace(redis=_Redis())
    client.cookies.set("devai_session", "platform-session")
    sid = client.post("/api/sandboxes", json=_SPEC, headers=_TENANT_A).json()["id"]

    assert client.get(f"/api/sandboxes/{sid}").status_code == 200


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
