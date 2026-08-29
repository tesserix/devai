from __future__ import annotations

from typing import Any

from devai.sandbox.evals import CaseResult, EvalRun, EvalStore


class _Database:
    def __init__(self) -> None:
        self.runs: dict[tuple[str, str, str], dict[str, Any]] = {}

    async def save_eval_run(self, run: dict[str, Any]) -> None:
        self.runs[(run["owner_scope"], run["sandbox_id"], run["id"])] = run

    async def get_eval_run(self, owner_scope: str, sandbox_id: str, run_id: str) -> dict[str, Any] | None:
        return self.runs.get((owner_scope, sandbox_id, run_id))

    async def get_eval_run_by_id(self, owner_scope: str, run_id: str) -> dict[str, Any] | None:
        return next(
            (run for (owner, _, stored_id), run in self.runs.items() if owner == owner_scope and stored_id == run_id),
            None,
        )

    async def list_eval_runs(self, owner_scope: str, sandbox_id: str, *, limit: int) -> list[dict[str, Any]]:
        return [
            run for (owner, sandbox, _), run in self.runs.items() if owner == owner_scope and sandbox == sandbox_id
        ][:limit]


async def test_durable_run_survives_store_and_sandbox_lifecycle_and_stays_user_scoped() -> None:
    database = _Database()
    run = EvalRun(
        id="eval-1",
        sandbox_id="sb-destroyed",
        agent="refund-agent",
        owner_scope="tenant-a:alice",
        tenant_id="tenant-a",
        user_id="alice",
        dataset_ref={"name": "golden", "version": "3"},
        suite_ref={"name": "release-gate", "version": "2"},
        results=[CaseResult(name="refund", passed=True)],
    )
    first_process = EvalStore(None, database=database)
    await first_process.save(run, ttl_seconds=1)

    restarted_after_sandbox_destruction = EvalStore(None, database=database)

    found = await restarted_after_sandbox_destruction.get(
        "sb-destroyed",
        "eval-1",
        owner_scope="tenant-a:alice",
    )
    foreign = await restarted_after_sandbox_destruction.get(
        "sb-destroyed",
        "eval-1",
        owner_scope="tenant-a:bob",
    )
    assert found is not None
    assert found.dataset_ref == {"name": "golden", "version": "3"}
    assert found.suite_ref == {"name": "release-gate", "version": "2"}
    assert foreign is None


async def test_durable_database_failure_fails_the_write_instead_of_losing_the_result() -> None:
    class _BrokenDatabase(_Database):
        async def save_eval_run(self, run: dict[str, Any]) -> None:
            del run
            raise ConnectionError("postgres unavailable")

    store = EvalStore(None, database=_BrokenDatabase())
    run = EvalRun(id="eval-1", sandbox_id="sb-1", owner_scope="tenant-a:alice")

    try:
        await store.save(run, ttl_seconds=1)
    except ConnectionError as error:
        assert str(error) == "postgres unavailable"
    else:
        raise AssertionError("durable write failure was swallowed")
