from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import devai.authz as authz
import devai.identity as identity
from devai.admin.routes import router
from devai.identity import Principal


def _client(principal: Principal | None, monkeypatch) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.state.usage_ledger = None
    app.state.analytics_db = None
    app.state.config = None

    async def _extract(_request):
        return principal

    monkeypatch.setattr(identity, "extract_principal", _extract)
    monkeypatch.setattr(authz, "extract_principal", _extract)
    return TestClient(app)


def _admin() -> Principal:
    return Principal(email="samyak.rout@gmail.com", uid="u-admin", roles=["admin"])


def _plain() -> Principal:
    return Principal(email="someone@example.com", uid="u-plain", roles=[])


# Every admin route must be listed here. A new route added without a test
# entry is caught by test_every_admin_route_is_covered below.
ADMIN_ROUTES = ["/api/admin/overview", "/api/admin/openpanel"]


@pytest.mark.parametrize("path", ADMIN_ROUTES)
def test_non_admin_is_forbidden(path, monkeypatch):
    res = _client(_plain(), monkeypatch).get(path)
    assert res.status_code == 403


@pytest.mark.parametrize("path", ADMIN_ROUTES)
def test_anonymous_is_unauthorized(path, monkeypatch):
    res = _client(None, monkeypatch).get(path)
    assert res.status_code == 401


@pytest.mark.parametrize("path", ADMIN_ROUTES)
def test_admin_is_allowed(path, monkeypatch):
    res = _client(_admin(), monkeypatch).get(path)
    assert res.status_code == 200


@pytest.mark.parametrize("path", ADMIN_ROUTES)
def test_platform_admin_is_allowed(path, monkeypatch):
    principal = Principal(email="p@example.com", uid="u-p", roles=["platform-admin"])
    res = _client(principal, monkeypatch).get(path)
    assert res.status_code == 200


def test_every_admin_route_is_covered():
    """A new /api/admin route must be added to ADMIN_ROUTES, so it inherits
    the 401/403 assertions above rather than shipping unguarded."""
    declared = {r.path for r in router.routes}
    assert declared == set(ADMIN_ROUTES)


class _Ledger:
    async def by_user(self, tenant: str = ""):
        return [{"user": "a@example.com", "user_id": "a", "tenant_id": tenant, "cost_usd": 1.5, "calls": 9}]


def _wired_client(monkeypatch) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.state.usage_ledger = _Ledger()
    app.state.analytics_db = None
    app.state.config = None

    async def _extract(_request):
        return _admin()

    monkeypatch.setattr(identity, "extract_principal", _extract)
    monkeypatch.setattr(authz, "extract_principal", _extract)

    import devai.admin.routes as admin_routes

    async def _timeseries(_db, days):
        return [{"date": "2026-08-29", "users": 2}]

    async def _signins(_db, days):
        return 4

    async def _totals(_db, days):
        return [{"user": "a@example.com", "days_active": 3, "last_seen": "2026-08-29"}]

    monkeypatch.setattr(admin_routes, "active_users_timeseries", _timeseries)
    monkeypatch.setattr(admin_routes, "signin_count", _signins)
    monkeypatch.setattr(admin_routes, "active_user_totals", _totals)
    return TestClient(app)


def test_overview_returns_all_sections(monkeypatch):
    res = _wired_client(monkeypatch).get("/api/admin/overview?days=7")
    assert res.status_code == 200
    body = res.json()
    assert body["days"] == 7
    assert body["active_users"] == [{"date": "2026-08-29", "users": 2}]
    assert body["signins"] == 4
    assert body["user_activity"][0]["user"] == "a@example.com"
    assert body["by_user"][0]["cost_usd"] == 1.5


def test_overview_without_a_ledger_still_returns_200(monkeypatch):
    client = _wired_client(monkeypatch)
    client.app.state.usage_ledger = None
    res = client.get("/api/admin/overview")
    assert res.status_code == 200
    assert res.json()["by_user"] == []


def test_openpanel_reports_disabled_when_unconfigured(monkeypatch):
    res = _wired_client(monkeypatch).get("/api/admin/openpanel")
    assert res.status_code == 200
    assert res.json()["enabled"] is False


def test_days_query_is_bounded(monkeypatch):
    assert _wired_client(monkeypatch).get("/api/admin/overview?days=0").status_code == 422
    assert _wired_client(monkeypatch).get("/api/admin/overview?days=400").status_code == 422
