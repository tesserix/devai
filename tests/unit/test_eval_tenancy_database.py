from __future__ import annotations

import json
from typing import Any

from devai.services.database import Database


class _Pool:
    def __init__(self) -> None:
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fetchrow_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fetch_calls: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, query: str, *args: Any) -> None:
        self.execute_calls.append((query, args))

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any]:
        self.fetchrow_calls.append((query, args))
        return {"evals": 0, "avg_score": 0.0, "pass_rate": 0.0}

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.fetch_calls.append((query, args))
        return []


def _database(pool: _Pool) -> Database:
    database = Database("")
    database._pool = pool
    return database


async def test_record_eval_persists_tenant_and_subject_in_detail() -> None:
    pool = _Pool()

    await _database(pool).record_eval(
        run_id="run-1",
        evaluator="security",
        score=1.0,
        passed=True,
        triggered_by="same@example.com",
        tenant_id="tenant-a",
        user_id="shared-uid",
        detail={"rule": "passed"},
    )

    detail = json.loads(pool.execute_calls[0][1][7])
    assert detail == {"rule": "passed", "tenant_id": "tenant-a", "user_id": "shared-uid"}


async def test_eval_analytics_filters_by_tenant_and_subject() -> None:
    pool = _Pool()

    await _database(pool).analytics_evals(7, tenant_id="tenant-a", user_id="shared-uid")

    query, args = pool.fetchrow_calls[0]
    assert "detail->>'tenant_id' = $2" in query
    assert "detail->>'user_id' = $3" in query
    assert args == (7, "tenant-a", "shared-uid")


async def test_tenant_admin_eval_analytics_never_reads_other_tenants() -> None:
    pool = _Pool()

    await _database(pool).analytics_evals(7, tenant_id="tenant-a")

    query, args = pool.fetchrow_calls[0]
    assert "detail->>'tenant_id' = $2" in query
    assert "detail->>'user_id'" not in query
    assert args == (7, "tenant-a")


async def test_lifecycle_eval_analytics_scopes_native_tenant_and_subject_columns() -> None:
    pool = _Pool()

    await _database(pool).analytics_lifecycle_eval_runs(7, tenant_id="tenant-a", user_id="shared-uid")

    query, args = pool.fetch_calls[0]
    assert "r.tenant_id = $2" in query
    assert "r.user_id = $3" in query
    assert args == (7, "tenant-a", "shared-uid", 200)
