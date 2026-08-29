"""Whichever snapshot is newer wins.

The API pod and the Temporal worker are separate processes. The API keeps the
task it enqueued in memory and never touches it again; the worker executing the
run persists its progress to shared state. Preferring in-memory unconditionally
therefore served a snapshot frozen at enqueue time — a run that was really
awaiting approval, with agents that had finished, rendered as "queued" with
"no agents yet".
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from devai.pipeline.service import PipelineService

STALE = {
    "id": "devai-1",
    "state": "queued",
    "updated_at": 100.0,
    "agents": {},
    "stage_events": [],
}
FRESH = {
    "id": "devai-1",
    "state": "awaiting_approval",
    "updated_at": 200.0,
    "agents": {"requirements_analyst": {"status": "completed"}},
    "stage_events": [{"stage": "analyze-requirements", "phase": "completed"}],
}


def _service(in_memory, persisted) -> PipelineService:
    svc = PipelineService.__new__(PipelineService)
    svc._pipeline = SimpleNamespace(
        get_task=lambda _id: None,
        list_tasks=lambda: [],
    )
    svc.get_task_in_memory = lambda _id: in_memory  # type: ignore[method-assign]
    svc.list_tasks_in_memory = lambda: [in_memory] if in_memory else []  # type: ignore[method-assign]

    async def get_persisted_task(_id):
        return persisted

    async def list_persisted_tasks(**_kwargs):
        return [persisted] if persisted else []

    svc.get_persisted_task = get_persisted_task  # type: ignore[method-assign]
    svc.list_persisted_tasks = list_persisted_tasks  # type: ignore[method-assign]
    return svc


@pytest.mark.asyncio
async def test_get_task_prefers_the_newer_snapshot():
    task = await _service(STALE, FRESH).get_task("devai-1")

    assert task["state"] == "awaiting_approval"
    assert task["agents"]


@pytest.mark.asyncio
async def test_get_task_keeps_in_memory_when_it_is_the_newer_one():
    task = await _service(FRESH, STALE).get_task("devai-1")

    assert task["state"] == "awaiting_approval"


@pytest.mark.asyncio
async def test_get_task_falls_back_when_only_one_side_has_it():
    assert (await _service(STALE, None).get_task("devai-1"))["state"] == "queued"
    assert (await _service(None, FRESH).get_task("devai-1"))["state"] == "awaiting_approval"


@pytest.mark.asyncio
async def test_get_task_returns_none_when_neither_side_has_it():
    assert await _service(None, None).get_task("devai-1") is None


@pytest.mark.asyncio
async def test_merged_listing_uses_the_newer_snapshot_per_run():
    rows = await _service(STALE, FRESH).list_runs(limit=50)

    assert [r["state"] for r in rows] == ["awaiting_approval"]
