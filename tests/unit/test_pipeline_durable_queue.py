"""Durable work-queue tests — the reliable-queue layer that keeps pipeline
runs from being orphaned when an api pod rolls or scales.

These cover the StateManager reliable-queue primitives (enqueue / claim /
heartbeat / ack / reclaim) and the DevAITask snapshot round-trip that lets any
replica rebuild and resume a run it didn't originally receive.

Why it matters: before this, the work queue was an in-memory asyncio.Queue per
pod, so a pod roll stranded queued runs forever in Redis with no owner. Now the
queue itself lives in Redis; any replica claims pending work and a dead pod's
work is reclaimed and resumed (the executor skips already-completed stages).
"""

from __future__ import annotations

import pytest

from devai.pipeline.types import DevAITask, StageEvent, StageEventPhase, TaskState


def _state_manager():
    """A StateManager backed by fakeredis (no real connection)."""
    fakeredis = pytest.importorskip("fakeredis")
    from devai.core.state import StateManager

    sm = StateManager.__new__(StateManager)
    sm.redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    sm.result_ttl = 3600
    sm.lock_ttl = 360
    return sm


# ── DevAITask snapshot round-trip ────────────────────────────────────────────


def test_devaitask_from_dict_roundtrip():
    t = DevAITask(intent="add feature X", blueprint="app-scaffold", repo="tesserix/test-repo")
    t.transition(TaskState.IMPLEMENTING)
    t.stages_completed = ["scan", "plan"]
    t.stages_skipped = ["lint"]
    t.current_stage = "implement"
    t.agent_context = {"coder_output": {"files": 3}}
    t.epic_issue_number = 42
    t.story_issue_numbers = [43, 44]
    t.principal = {"email": "samyak.rout@gmail.com"}
    t.record_event(StageEvent("scan", StageEventPhase.COMPLETED, duration_ms=1200.0, message="done"))
    t.label = "add feature X"

    restored = DevAITask.from_dict(t.to_dict())

    assert restored.to_dict() == t.to_dict()
    assert restored.state is TaskState.IMPLEMENTING
    assert restored.stages_completed == ["scan", "plan"]
    assert restored.stage_events[0].phase is StageEventPhase.COMPLETED
    assert restored.principal == {"email": "samyak.rout@gmail.com"}


def test_devaitask_from_dict_tolerates_partial_snapshot():
    restored = DevAITask.from_dict({"id": "devai-legacy", "blueprint": "alm-pipeline"})
    assert restored.id == "devai-legacy"
    assert restored.state is TaskState.PENDING
    assert restored.stages_completed == []


def test_devaitask_from_dict_bad_state_falls_back_to_pending():
    restored = DevAITask.from_dict({"id": "x", "state": "not-a-real-state"})
    assert restored.state is TaskState.PENDING


# ── Reliable-queue primitives ────────────────────────────────────────────────


async def test_enqueue_is_idempotent():
    sm = _state_manager()
    first = await sm.enqueue_task("devai-1")
    dup = await sm.enqueue_task("devai-1")  # another replica reconciling
    assert first is True
    assert dup is False
    assert await sm.is_task_active("devai-1") is True


async def test_claim_is_exactly_once_then_times_out():
    sm = _state_manager()
    await sm.enqueue_task("devai-1")
    await sm.enqueue_task("devai-2")

    r1 = await sm.claim_next_task("workerA", timeout=1)
    r2 = await sm.claim_next_task("workerB", timeout=1)
    assert {r1, r2} == {"devai-1", "devai-2"}

    # Queue drained — a further claim blocks briefly then returns None.
    assert await sm.claim_next_task("workerC", timeout=1) is None


async def test_claim_stamps_liveness_and_ack_clears_everything():
    sm = _state_manager()
    await sm.enqueue_task("devai-1")
    claimed = await sm.claim_next_task("workerA", timeout=1, claim_ttl=50)
    assert claimed == "devai-1"
    assert await sm.redis.exists("devai:pipeline:claim:devai-1")

    await sm.ack_task("devai-1")
    assert await sm.is_task_active("devai-1") is False
    assert not await sm.redis.exists("devai:pipeline:claim:devai-1")
    assert await sm.redis.lrem("devai:pipeline:processing", 0, "devai-1") == 0


async def test_reaper_requeues_dead_owner_only():
    sm = _state_manager()
    await sm.enqueue_task("alive")
    await sm.enqueue_task("dead")
    await sm.claim_next_task("workerA", timeout=1)  # alive
    await sm.claim_next_task("workerB", timeout=1)  # dead

    # Both claims live → nothing reclaimed.
    assert await sm.reclaim_stale_tasks() == []

    # Owner of "dead" disappears (claim TTL expired).
    await sm.redis.delete("devai:pipeline:claim:dead")
    assert await sm.reclaim_stale_tasks() == ["dead"]

    # It's back on the queue, still active, and claimable again.
    assert await sm.is_task_active("dead") is True
    assert await sm.claim_next_task("workerC", timeout=1) == "dead"


async def test_double_reap_does_not_duplicate():
    sm = _state_manager()
    await sm.enqueue_task("dead")
    await sm.claim_next_task("workerA", timeout=1)
    await sm.redis.delete("devai:pipeline:claim:dead")

    first = await sm.reclaim_stale_tasks()
    second = await sm.reclaim_stale_tasks()  # a second replica's reaper, same sweep
    assert first == ["dead"]
    assert second == []  # LREM count guard prevents a duplicate requeue
    # Exactly one copy on the queue.
    assert await sm.redis.lrange("devai:pipeline:queue", 0, -1) == ["dead"]
