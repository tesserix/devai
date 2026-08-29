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
ADMIN_ROUTES = ["/api/admin/overview"]


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
