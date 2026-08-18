"""HTTP tests for the settings API: auth, authorization, and secret guards."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

import devai.settings.routes as settings_routes
from devai.identity import Principal


class _FakeSecrets:
    def __init__(self, writable: bool) -> None:
        self._w = writable
        self.saved: dict = {}

    async def can_write(self):
        return self._w


class _FakeSvc:
    def __init__(self, writable=True):
        self._secrets = _FakeSecrets(writable)
        self.has_db = False
        self.connectors: list = []

    async def secrets_writable(self):
        return await self._secrets.can_write()

    async def list_connectors(self, scope, scope_id):
        return []

    async def upsert_connector(self, **kw):
        from devai.settings.models import Connector, Scope

        c = Connector(
            scope=Scope(kw["scope"].value if hasattr(kw["scope"], "value") else kw["scope"]),
            scope_id=kw["scope_id"],
            connector_key=kw["connector_key"],
            provider=kw["provider"],
        )
        self.connectors.append(c)
        return c

    async def delete_connector(self, *a, **k):
        return True


def _app(principal: Principal | None, writable=True, *, gateway_required=False):
    app = FastAPI()
    app.include_router(settings_routes.router)
    app.state.settings_service = _FakeSvc(writable)
    app.state.config = SimpleNamespace(llm_gateway_required=gateway_required)

    async def _fake_extract(_request):
        return principal

    settings_routes.extract_principal = _fake_extract  # type: ignore[assignment]
    return app


ALICE = Principal(email="alice@x.com", uid="alice-uid", roles=[], team_ids=["teamA"])
TENANT_ALICE = Principal(email="alice@x.com", uid="alice-uid", tenant_id="tenant-a", roles=[])
ADMIN = Principal(email="admin@x.com", uid="admin", roles=["platform-admin"])
TENANT_ADMIN = Principal(email="admin@x.com", uid="admin", tenant_id="tenant-a", roles=["admin"])


def test_requires_auth():
    client = TestClient(_app(None))
    assert client.get("/api/settings/catalog").status_code == 401
    assert client.get("/api/settings").status_code == 401


def test_catalog_returns_connectors():
    client = TestClient(_app(ALICE))
    r = client.get("/api/settings/catalog")
    assert r.status_code == 200
    body = r.json()
    keys = {c["key"] for c in body["connectors"]}
    assert {"llm", "scm", "mcp"} <= keys
    assert body["secrets_writable"] is True


def test_user_can_save_own_scope():
    client = TestClient(_app(ALICE))
    r = client.post(
        "/api/settings/connectors",
        json={"connector_key": "llm", "provider": "openai", "scope": "user"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "saved"


def test_tenant_user_scope_is_saved_with_tenant_qualified_subject():
    app = _app(TENANT_ALICE)
    client = TestClient(app)

    response = client.post(
        "/api/settings/connectors",
        json={"connector_key": "llm", "provider": "openai", "scope": "user"},
    )

    assert response.status_code == 200
    assert app.state.settings_service.connectors[0].scope_id == "tenant-a:alice-uid"


def test_tenant_user_cannot_manage_same_uid_in_another_tenant():
    client = TestClient(_app(TENANT_ALICE))

    response = client.delete("/api/settings/connectors/user/tenant-b%3Aalice-uid/llm")

    assert response.status_code == 404


def test_strict_gateway_mode_rejects_direct_llm_provider():
    client = TestClient(_app(TENANT_ALICE, gateway_required=True))

    response = client.post(
        "/api/settings/connectors",
        json={"connector_key": "llm", "provider": "anthropic", "scope": "user"},
    )

    assert response.status_code == 409
    assert "gateway" in response.json()["detail"].lower()


def test_strict_gateway_mode_accepts_gateway_connector():
    client = TestClient(_app(TENANT_ALICE, gateway_required=True))

    response = client.post(
        "/api/settings/connectors",
        json={"connector_key": "llm", "provider": "gateway", "scope": "user"},
    )

    assert response.status_code == 200


def test_user_cannot_save_global():
    client = TestClient(_app(ALICE))
    r = client.post(
        "/api/settings/connectors",
        json={"connector_key": "llm", "provider": "openai", "scope": "global"},
    )
    assert r.status_code == 403


def test_admin_can_save_global():
    client = TestClient(_app(ADMIN))
    r = client.post(
        "/api/settings/connectors",
        json={"connector_key": "llm", "provider": "openai", "scope": "global"},
    )
    assert r.status_code == 200


def test_tenant_admin_can_save_only_own_tenant():
    client = TestClient(_app(TENANT_ADMIN))

    own = client.post(
        "/api/settings/connectors",
        json={"connector_key": "llm", "provider": "openai", "scope": "tenant", "scope_id": "tenant-a"},
    )
    foreign = client.post(
        "/api/settings/connectors",
        json={"connector_key": "llm", "provider": "openai", "scope": "tenant", "scope_id": "tenant-b"},
    )
    global_response = client.post(
        "/api/settings/connectors",
        json={"connector_key": "llm", "provider": "openai", "scope": "global"},
    )

    assert own.status_code == 200
    assert foreign.status_code == 404
    assert global_response.status_code == 403


def test_secret_write_blocked_when_readonly():
    client = TestClient(_app(ALICE, writable=False))
    r = client.post(
        "/api/settings/connectors",
        json={
            "connector_key": "llm",
            "provider": "anthropic",
            "scope": "user",
            "secrets": {"anthropic_api_key": "sk-ant-xxx"},
        },
    )
    assert r.status_code == 409


def test_unknown_connector_rejected():
    client = TestClient(_app(ALICE))
    r = client.post("/api/settings/connectors", json={"connector_key": "nope", "provider": "x", "scope": "user"})
    assert r.status_code == 400


def test_user_cannot_save_foreign_team():
    client = TestClient(_app(ALICE))
    r = client.post(
        "/api/settings/connectors",
        json={"connector_key": "llm", "provider": "openai", "scope": "team", "scope_id": "teamZ"},
    )
    assert r.status_code == 403


def test_models_discovery_unknown_provider_400():
    client = TestClient(_app(ALICE))
    r = client.get("/api/settings/models/no-such-provider")
    assert r.status_code == 400


def test_models_discovery_unconfigured_provider_reports_not_configured():
    client = TestClient(_app(ALICE))
    # groq has no key in test settings → noop adapter → configured: false
    r = client.get("/api/settings/models/groq")
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is False
    assert body["models"] == []


def test_models_discovery_requires_auth():
    client = TestClient(_app(None))
    assert client.get("/api/settings/models/anthropic").status_code == 401
