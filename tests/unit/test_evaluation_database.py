from __future__ import annotations

from typing import Any

from devai.services.database import Database


class _Context:
    def __init__(self, value: Any) -> None:
        self.value = value

    async def __aenter__(self) -> Any:
        return self.value

    async def __aexit__(self, *args: Any) -> None:
        del args


class _Connection:
    def __init__(self, rows: list[dict[str, Any] | None]) -> None:
        self.rows = list(rows)
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []

    def transaction(self) -> _Context:
        return _Context(self)

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        self.calls.append((query, args))
        return self.rows.pop(0)

    async def execute(self, query: str, *args: Any) -> None:
        self.execute_calls.append((query, args))


class _Pool:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection
        self.fetchrow_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fetch_calls: list[tuple[str, tuple[Any, ...]]] = []

    def acquire(self) -> _Context:
        return _Context(self.connection)

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        self.fetchrow_calls.append((query, args))
        return None

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.fetch_calls.append((query, args))
        return []


def _database(rows: list[dict[str, Any] | None] | None = None) -> tuple[Database, _Pool]:
    pool = _Pool(_Connection(rows or []))
    database = Database("")
    database._pool = pool
    return database, pool


async def test_dataset_creation_keeps_parent_and_immutable_version_in_one_transaction() -> None:
    database, pool = _database(
        [
            {"id": "dataset-id"},
            {
                "name": "golden",
                "version": "3",
                "description": "",
                "case_count": 1,
                "content_hash": "abc",
                "blob_key": "evaluations/datasets/sha256/abc.json",
                "owner_scope": "tenant-a:alice",
                "created_at": "now",
            },
        ]
    )

    row = await database.create_eval_dataset_version(
        owner_scope="tenant-a:alice",
        tenant_id="tenant-a",
        user_id="alice",
        name="golden",
        version="3",
        description="",
        case_count=1,
        content_hash="abc",
        blob_key="evaluations/datasets/sha256/abc.json",
    )

    assert row is not None
    assert len(pool.connection.calls) == 2
    assert "ON CONFLICT (owner_scope, name)" in pool.connection.calls[0][0]
    assert "ON CONFLICT (dataset_id, version) DO NOTHING" in pool.connection.calls[1][0]
    assert pool.connection.calls[0][1][:3] == ("tenant-a:alice", "tenant-a", "alice")


async def test_dataset_reads_scope_the_lookup_in_sql_not_after_fetch() -> None:
    database, pool = _database()

    await database.get_eval_dataset_version("tenant-a:alice", "golden", "3")

    query, args = pool.fetchrow_calls[0]
    assert "d.owner_scope = $1" in query
    assert "d.name = $2" in query
    assert "v.version = $3" in query
    assert args == ("tenant-a:alice", "golden", "3")
    assert "v.description" in query
    assert "d.description" not in query


async def test_suite_creation_resolves_the_dataset_version_in_the_same_owner_scope() -> None:
    database, pool = _database(
        [
            {
                "name": "gate",
                "version": "2",
                "description": "",
                "dataset_name": "golden",
                "dataset_version": "3",
                "scorers": ["exact_match"],
                "thresholds": {},
                "owner_scope": "tenant-a:alice",
                "created_at": "now",
            }
        ]
    )

    await database.create_eval_suite(
        owner_scope="tenant-a:alice",
        tenant_id="tenant-a",
        user_id="alice",
        name="gate",
        version="2",
        description="",
        dataset_name="golden",
        dataset_version="3",
        scorers=["exact_match"],
        thresholds={},
    )

    query, args = pool.connection.calls[0]
    assert "d.owner_scope = $1" in query
    assert "d.name = $7" in query
    assert "dv.version = $8" in query
    assert "ON CONFLICT (owner_scope, name, version) DO NOTHING" in query
    assert args[0] == "tenant-a:alice"


async def test_eval_run_and_case_results_commit_together_with_exact_version_ids() -> None:
    database, pool = _database([{"suite_id": "suite-id", "dataset_version_id": "dataset-version-id"}])

    await database.save_eval_run(
        {
            "id": "eval-1",
            "owner_scope": "tenant-a:alice",
            "tenant_id": "tenant-a",
            "user_id": "alice",
            "sandbox_id": "sb-1",
            "agent": "refund-agent",
            "dataset": {"name": "golden", "version": "3"},
            "suite": {"name": "gate", "version": "2"},
            "created_at": "2026-08-19T00:00:00+00:00",
            "summary": {"cases": 1, "passed": 1},
            "results": [{"name": "refund", "passed": True, "failures": []}],
        }
    )

    resolve_query, resolve_args = pool.connection.calls[0]
    assert "s.owner_scope = $1" in resolve_query
    assert "s.name = $2 AND s.version = $3" in resolve_query
    assert resolve_args == ("tenant-a:alice", "gate", "2", "golden", "3")
    assert "INSERT INTO eval_runs" in pool.connection.execute_calls[0][0]
    assert pool.connection.execute_calls[0][1][1:4] == ("tenant-a:alice", "tenant-a", "alice")
    assert "INSERT INTO eval_case_results" in pool.connection.execute_calls[1][0]


async def test_eval_run_reads_are_owner_scoped_in_the_query() -> None:
    database, pool = _database()

    await database.get_eval_run("tenant-a:alice", "sb-1", "eval-1")
    await database.list_eval_runs("tenant-a:alice", "sb-1", limit=20)

    get_query, get_args = pool.fetchrow_calls[0]
    list_query, list_args = pool.fetch_calls[0]
    assert "r.owner_scope = $1" in get_query
    assert "r.sandbox_id = $2 AND r.id = $3" in get_query
    assert get_args == ("tenant-a:alice", "sb-1", "eval-1")
    assert "r.owner_scope = $1" in list_query
    assert "r.sandbox_id = $2" in list_query
    assert list_args == ("tenant-a:alice", "sb-1", 20)


async def test_top_level_eval_run_lookup_scopes_the_id_in_sql() -> None:
    database, pool = _database()

    await database.get_eval_run_by_id("tenant-a:alice", "eval-1")

    query, args = pool.fetchrow_calls[0]
    assert "r.owner_scope = $1 AND r.id = $2" in query
    assert args == ("tenant-a:alice", "eval-1")
