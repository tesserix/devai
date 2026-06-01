"""Tests for the opt-in auth gate (DEVAI_REQUIRE_AUTH)."""

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from devai.authz import enforce_auth, needs_auth_check


def test_needs_auth_check_only_mutating():
    assert needs_auth_check("POST", "/api/pipeline/runs")
    assert needs_auth_check("DELETE", "/api/scm/onboarded/o/r")
    assert not needs_auth_check("GET", "/api/pipeline/runs")
    assert not needs_auth_check("HEAD", "/anything")


def test_needs_auth_check_exempts_webhook_and_health():
    assert not needs_auth_check("POST", "/webhook/github")
    assert not needs_auth_check("POST", "/healthz")
    assert not needs_auth_check("POST", "/readyz")


def _app(require_auth: bool) -> FastAPI:
    app = FastAPI()
    app.state.config = SimpleNamespace(require_auth=require_auth)

    @app.middleware("http")
    async def gate(request, call_next):
        blocked = await enforce_auth(request)
        if blocked is not None:
            return blocked
        return await call_next(request)

    @app.post("/api/thing")
    async def thing():
        return {"ok": True}

    @app.get("/api/thing")
    async def get_thing():
        return {"ok": True}

    @app.post("/webhook/github")
    async def hook():
        return {"ok": True}

    return app


def test_gate_disabled_allows_anonymous_post():
    client = TestClient(_app(require_auth=False))
    assert client.post("/api/thing").status_code == 200


def test_gate_enabled_blocks_anonymous_post():
    client = TestClient(_app(require_auth=True))
    r = client.post("/api/thing")
    assert r.status_code == 401
    assert r.json()["detail"] == "authentication required"


def test_gate_enabled_allows_reads():
    client = TestClient(_app(require_auth=True))
    assert client.get("/api/thing").status_code == 200


def test_gate_enabled_exempts_webhook():
    client = TestClient(_app(require_auth=True))
    assert client.post("/webhook/github").status_code == 200


def test_gate_enabled_allows_authenticated_post():
    client = TestClient(_app(require_auth=True))
    r = client.post("/api/thing", headers={"X-Forwarded-User": "alice@example.com"})
    assert r.status_code == 200
