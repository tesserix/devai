from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

import devai.authz as authz
import devai.identity as identity
from devai.analytics.routes import router
from devai.identity import Principal


class _Ledger:
    async def summary(self, user: str = "", tenant: str = ""):
        return {"calls": 1, "tenant": tenant, "user": user}

    async def by_model(self, user: str = "", tenant: str = ""):
        return []

    async def by_user(self, tenant: str = ""):
        return [{"tenant": tenant}]

    async def by_sandbox(self, tenant: str = "", user: str = ""):
        return [{"tenant": tenant, "user": user}]

    async def timeseries(self, days: int = 30, user: str = "", tenant: str = ""):
        return []

    async def recent(self, limit: int = 100, user: str = "", tenant: str = ""):
        return [{"tenant": tenant, "user": user}]


class _Database:
    def __init__(self) -> None:
        self.calls = []

    async def analytics_llm_cost_by_model(self, days: int, *, tenant_id: str = "", user_id: str = ""):
        self.calls.append(("model", days, tenant_id, user_id))
        return []

    async def analytics_llm_cost_timeseries(self, days: int, *, tenant_id: str = "", user_id: str = ""):
        self.calls.append(("timeseries", days, tenant_id, user_id))
        return []

    async def analytics_agent_stats(self, days: int, *, tenant_id: str = "", user_id: str = ""):
        self.calls.append(("agents", days, tenant_id, user_id))
        return []

    async def analytics_evals(self, days: int, *, tenant_id: str = "", user_id: str = ""):
        self.calls.append(("evals", days, tenant_id, user_id))
        return {"summary": {"evals": 1, "avg_score": 1.0, "pass_rate": 1.0}, "by_evaluator": [], "recent": []}


def _app(principal: Principal | None, monkeypatch) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.usage_ledger = _Ledger()
    app.state.analytics_db = _Database()

    async def _extract(_request):
        return principal

    monkeypatch.setattr(identity, "extract_principal", _extract)
    monkeypatch.setattr(authz, "extract_principal", _extract)
    return app


def _client(principal: Principal | None, monkeypatch) -> TestClient:
    app = _app(principal, monkeypatch)
    return TestClient(app)


def test_usage_rejects_unauthenticated_instead_of_reading_global_namespace(monkeypatch):
    client = _client(None, monkeypatch)

    assert client.get("/api/analytics/usage").status_code == 401
    assert client.get("/api/analytics/usage/recent").status_code == 401
    assert client.get("/api/analytics/llm/cost").status_code == 401
    assert client.get("/api/analytics/agents").status_code == 401
    assert client.get("/api/analytics/evals").status_code == 401


def test_user_usage_is_scoped_by_tenant_and_subject(monkeypatch):
    client = _client(Principal(email="same@example.com", uid="shared-uid", tenant_id="tenant-a"), monkeypatch)

    response = client.get("/api/analytics/usage")

    assert response.status_code == 200
    assert response.json()["summary"] == {"calls": 1, "tenant": "tenant-a", "user": "shared-uid"}
    assert response.json()["by_user"] == []
    assert response.json()["by_sandbox"] == [{"tenant": "tenant-a", "user": "shared-uid"}]


def test_tenant_admin_sees_only_tenant_rollup(monkeypatch):
    client = _client(
        Principal(email="admin@example.com", uid="admin-uid", tenant_id="tenant-a", roles=["admin"]),
        monkeypatch,
    )

    response = client.get("/api/analytics/usage")

    assert response.status_code == 200
    assert response.json()["summary"] == {"calls": 1, "tenant": "tenant-a", "user": ""}
    assert response.json()["by_user"] == [{"tenant": "tenant-a"}]
    assert response.json()["by_sandbox"] == [{"tenant": "tenant-a", "user": ""}]


def test_postgres_cost_and_agent_rollups_receive_the_same_principal_scope(monkeypatch):
    app = _app(Principal(email="same@example.com", uid="shared-uid", tenant_id="tenant-a"), monkeypatch)
    client = TestClient(app)

    assert client.get("/api/analytics/llm/cost?days=7").status_code == 200
    assert client.get("/api/analytics/agents?days=7").status_code == 200

    assert app.state.analytics_db.calls == [
        ("model", 7, "tenant-a", "shared-uid"),
        ("timeseries", 7, "tenant-a", "shared-uid"),
        ("agents", 7, "tenant-a", "shared-uid"),
    ]


def test_eval_rollups_receive_the_same_tenant_and_subject_scope(monkeypatch):
    app = _app(Principal(email="same@example.com", uid="shared-uid", tenant_id="tenant-a"), monkeypatch)
    client = TestClient(app)

    response = client.get("/api/analytics/evals?days=7")

    assert response.status_code == 200
    assert response.json()["scope"] == "me"
    assert app.state.analytics_db.calls == [("evals", 7, "tenant-a", "shared-uid")]


def test_tenant_admin_eval_rollup_is_tenant_scoped(monkeypatch):
    app = _app(
        Principal(email="admin@example.com", uid="admin-uid", tenant_id="tenant-a", roles=["admin"]),
        monkeypatch,
    )
    client = TestClient(app)

    response = client.get("/api/analytics/evals?days=7")

    assert response.status_code == 200
    assert response.json()["scope"] == "tenant"
    assert app.state.analytics_db.calls == [("evals", 7, "tenant-a", "")]
