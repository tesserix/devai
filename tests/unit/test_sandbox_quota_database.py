from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from devai.services.database import Database, SandboxQuotaExceeded


class _Context:
    def __init__(self, value: Any) -> None:
        self.value = value

    async def __aenter__(self) -> Any:
        return self.value

    async def __aexit__(self, *args: Any) -> None:
        del args


class _Connection:
    def __init__(self, values: list[Any]) -> None:
        self.values = list(values)
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    def transaction(self) -> _Context:
        return _Context(self)

    async def execute(self, query: str, *args: Any) -> None:
        self.executed.append((query, args))

    async def fetchval(self, query: str, *args: Any) -> Any:
        self.executed.append((query, args))
        return self.values.pop(0)


class _Pool:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    def acquire(self) -> _Context:
        return _Context(self.connection)


def _database(values: list[Any]) -> tuple[Database, _Connection]:
    connection = _Connection(values)
    database = Database("")
    database._pool = _Pool(connection)
    return database, connection


async def _create(database: Database) -> None:
    now = datetime.now(UTC)
    await database.create_sandbox(
        sandbox_id="sb-1",
        owner="tenant-a:subject-a",
        spec={"agent": {}, "model": {}},
        status="pending",
        created_at=now,
        expires_at=now + timedelta(hours=1),
        tenant_id="tenant-a",
        user_id="subject-a",
        max_live_per_tenant=5,
        monthly_cost_limit_usd=25.0,
    )


async def test_concurrent_quota_is_checked_under_the_tenant_advisory_lock() -> None:
    database, connection = _database([5])

    with pytest.raises(SandboxQuotaExceeded, match="concurrent sandbox quota"):
        await _create(database)

    assert "pg_advisory_xact_lock" in connection.executed[0][0]
    assert not any("INSERT INTO sandboxes" in query for query, _ in connection.executed)


async def test_monthly_cost_quota_refuses_creation_before_insert() -> None:
    database, connection = _database([0, 25.0])

    with pytest.raises(SandboxQuotaExceeded, match="monthly sandbox cost quota"):
        await _create(database)

    assert not any("INSERT INTO sandboxes" in query for query, _ in connection.executed)


async def test_quota_check_and_insert_share_one_transaction() -> None:
    database, connection = _database([0, 12.5])

    await _create(database)

    assert "INSERT INTO sandboxes" in connection.executed[-1][0]
