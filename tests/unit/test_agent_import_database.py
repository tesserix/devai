from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Any

import pytest

from devai.services.database import Database


class _Context(AbstractAsyncContextManager[Any]):
    def __init__(self, value: Any) -> None:
        self.value = value

    async def __aenter__(self) -> Any:
        return self.value

    async def __aexit__(self, *args: Any) -> None:
        return None


class _Connection:
    def __init__(self, *, insert_wins: bool = True) -> None:
        self.insert_wins = insert_wins
        self.fetches: list[tuple[str, tuple[Any, ...]]] = []
        self.executes: list[tuple[str, tuple[Any, ...]]] = []
        self.existing: dict[str, Any] = {}

    def transaction(self) -> _Context:
        return _Context(self)

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        self.fetches.append((query, args))
        if "INSERT INTO agent_imports" in query:
            if not self.insert_wins:
                return None
            return _row(args)
        return self.existing or None

    async def execute(self, query: str, *args: Any) -> str:
        self.executes.append((query, args))
        return "INSERT 0 1"


class _Pool:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    def acquire(self) -> _Context:
        return _Context(self.connection)


def _row(args: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "id": args[0],
        "owner_scope": args[1],
        "tenant_id": args[2],
        "project_id": args[3],
        "idempotency_key": args[4],
        "request_fingerprint": args[5],
        "registry_ref": args[6],
        "state": args[7],
        "agent": args[8],
        "dependency_lock": args[9],
        "permissions": args[10],
        "conformance": args[11],
        "created_by": args[12],
        "created_at": args[13],
        "updated_at": args[14],
    }


def _values() -> dict[str, Any]:
    now = datetime.now(UTC)
    return {
        "id": "bf2ef27d-98a2-4ce4-b87a-c6952d2d5d09",
        "owner_scope": "acme",
        "tenant_id": "acme",
        "project_id": "support-lab",
        "idempotency_key": "publish-run-42",
        "request_fingerprint": "sha256:" + "f" * 64,
        "registry_ref": "registry://acme/agents/acme/support@1.4.0",
        "state": "ready",
        "agent": {"name": "support"},
        "dependency_lock": [{"name": "triage"}],
        "permissions": {},
        "conformance": {"level": "callable"},
        "created_by": "acme:alice",
        "created_at": now,
        "updated_at": now,
    }


@pytest.mark.asyncio
async def test_create_agent_import_commits_snapshot_and_outbox_together() -> None:
    connection = _Connection()
    database = Database("postgres://unused")
    database._pool = _Pool(connection)  # noqa: SLF001 - isolated persistence contract

    row = await database.create_agent_import(**_values())

    assert row["agent"] == {"name": "support"}
    assert any("INSERT INTO agent_import_outbox" in query for query, _ in connection.executes)


@pytest.mark.asyncio
async def test_create_agent_import_returns_unique_key_winner_without_second_outbox() -> None:
    connection = _Connection(insert_wins=False)
    connection.existing = _values()
    database = Database("postgres://unused")
    database._pool = _Pool(connection)  # noqa: SLF001 - isolated persistence contract

    row = await database.create_agent_import(**_values())

    assert row["id"] == _values()["id"]
    assert connection.executes == []


@pytest.mark.asyncio
async def test_lifecycle_transition_and_outbox_intent_commit_in_one_transaction() -> None:
    class _LifecycleConnection:
        def __init__(self) -> None:
            self.fetches: list[tuple[str, tuple[Any, ...]]] = []
            self.executes: list[tuple[str, tuple[Any, ...]]] = []

        def transaction(self) -> _Context:
            return _Context(self)

        async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
            self.fetches.append((query, args))
            return {
                "id": args[0],
                "workflow_id": args[1],
                "sequence": args[2],
                "owner_scope": args[3],
                "tenant_id": args[4],
                "operation": args[5],
                "state": args[6],
                "step": args[7],
                "error_code": args[8],
                "created_at": args[9],
            }

        async def execute(self, query: str, *args: Any) -> str:
            self.executes.append((query, args))
            return "INSERT 0 1"

    connection = _LifecycleConnection()
    database = Database("postgres://unused")
    database._pool = _Pool(connection)  # noqa: SLF001 - isolated persistence contract
    now = datetime.now(UTC)

    row = await database.record_agent_lifecycle_event(
        id="bf2ef27d-98a2-4ce4-b87a-c6952d2d5d09",
        workflow_id="agent-eval:tenant-a:agent-lab:digest",
        sequence=2,
        owner_scope="tenant-a:alice",
        tenant_id="tenant-a",
        operation="evaluate",
        state="running",
        step="run_evaluation",
        error_code="",
        created_at=now,
    )

    assert row["workflow_id"] == "agent-eval:tenant-a:agent-lab:digest"
    assert "INSERT INTO agent_lifecycle_events" in connection.fetches[0][0]
    assert "ON CONFLICT (workflow_id, sequence) DO NOTHING" in connection.fetches[0][0]
    assert len(connection.executes) == 1
    assert "INSERT INTO agent_lifecycle_outbox" in connection.executes[0][0]
    assert "ON CONFLICT (event_id, event_type) DO NOTHING" in connection.executes[0][0]


@pytest.mark.asyncio
async def test_lifecycle_outbox_reads_unpublished_rows_in_order_and_marks_by_id() -> None:
    now = datetime.now(UTC)

    class _RelayPool:
        def __init__(self) -> None:
            self.fetch_call: tuple[str, tuple[Any, ...]] | None = None
            self.execute_call: tuple[str, tuple[Any, ...]] | None = None

        async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
            self.fetch_call = (query, args)
            return [
                {
                    "id": "outbox-1",
                    "event_id": "event-1",
                    "event_type": "agent_lifecycle.transitioned",
                    "payload": {"state": "running"},
                    "tenant_id": "acme",
                    "created_at": now,
                }
            ]

        async def execute(self, query: str, *args: Any) -> str:
            self.execute_call = (query, args)
            return "UPDATE 1"

    pool = _RelayPool()
    database = Database("postgres://unused")
    database._pool = pool  # type: ignore[assignment]  # isolated persistence contract

    rows = await database.pending_agent_lifecycle_outbox(limit=25)
    await database.mark_agent_lifecycle_outbox_published("outbox-1", published_at=now)

    assert rows[0]["tenant_id"] == "acme"
    assert pool.fetch_call is not None
    assert "published_at IS NULL" in pool.fetch_call[0]
    assert "ORDER BY o.created_at, o.id" in pool.fetch_call[0]
    assert pool.fetch_call[1] == (25,)
    assert pool.execute_call is not None
    assert "published_at IS NULL" in pool.execute_call[0]
    assert pool.execute_call[1] == ("outbox-1", now)


@pytest.mark.asyncio
async def test_lifecycle_operational_snapshot_reports_current_backlogs_without_tenant_labels() -> None:
    class _MetricsPool:
        query = ""

        async def fetchrow(self, query: str) -> dict[str, Any]:
            self.query = query
            return {
                "live_sandboxes": 8,
                "pending_sandboxes": 2,
                "destroying_sandboxes": 1,
                "cleanup_backlog": 3,
                "stuck_workflows": 1,
                "outbox_pending": 4,
                "outbox_oldest_age_seconds": 31.5,
            }

    pool = _MetricsPool()
    database = Database("postgres://unused")
    database._pool = pool  # type: ignore[assignment]  # isolated persistence contract

    snapshot = await database.agent_lifecycle_operational_snapshot()

    assert snapshot["stuck_workflows"] == 1.0
    assert snapshot["cleanup_backlog"] == 3.0
    assert snapshot["outbox_oldest_age_seconds"] == 31.5
    assert "DISTINCT ON (workflow_id)" in pool.query
    assert "published_at IS NULL" in pool.query
    assert "tenant_id" not in snapshot
