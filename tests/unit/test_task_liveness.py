"""Worker-liveness primitives — the contract that prevents orphaned runs.

Pins the incident class permanently: a hard-killed worker (pod roll, OOM)
must never strand a run, a live worker must never be double-executed, and a
graceful shutdown must hand the run to the next pod immediately.
"""

from __future__ import annotations

import sys
import types

import pytest

# devai.models imports `ulid` (optional dep absent in slim envs) on the
# state-manager import chain; stub it so this contract suite always runs.
if "ulid" not in sys.modules:
    _ulid = types.ModuleType("ulid")
    _ulid.ULID = type("ULID", (), {"__str__": lambda self: "01TEST"})
    sys.modules["ulid"] = _ulid

from devai.core.state import StateManager


class _FakePipe:
    def __init__(self, store: _FakeRedis) -> None:
        self._store = store
        self._ops: list = []

    def __getattr__(self, name):
        def _queue(*args, **kwargs):
            self._ops.append((name, args, kwargs))
            return self

        return _queue

    async def execute(self):
        out = []
        for name, args, kwargs in self._ops:
            out.append(await getattr(self._store, name)(*args, **kwargs))
        self._ops.clear()
        return out


class _FakeRedis:
    """Just enough redis for the liveness surface."""

    def __init__(self) -> None:
        self.sets: dict[str, set] = {}
        self.kv: dict[str, str] = {}
        self.lists: dict[str, list] = {}

    def pipeline(self, *a, **k):
        return _FakePipe(self)

    async def sismember(self, key, member):
        return member in self.sets.get(key, set())

    async def sadd(self, key, member):
        s = self.sets.setdefault(key, set())
        if member in s:
            return 0
        s.add(member)
        return 1

    async def srem(self, key, member):
        s = self.sets.get(key, set())
        if member in s:
            s.discard(member)
            return 1
        return 0

    async def exists(self, key):
        return 1 if key in self.kv else 0

    async def set(self, key, value, ex=None):
        self.kv[key] = value
        return True

    async def delete(self, key):
        return 1 if self.kv.pop(key, None) is not None else 0

    async def lpush(self, key, value):
        self.lists.setdefault(key, []).insert(0, value)
        return len(self.lists[key])

    async def lrem(self, key, count, value):
        lst = self.lists.get(key, [])
        n = lst.count(value)
        self.lists[key] = [v for v in lst if v != value]
        return n

    async def lpos(self, key, value):
        lst = self.lists.get(key, [])
        return lst.index(value) if value in lst else None


def _sm() -> StateManager:
    sm = StateManager.__new__(StateManager)  # skip __init__ (no real redis)
    sm.redis = _FakeRedis()
    return sm


@pytest.mark.asyncio
async def test_live_worker_is_live_and_protected():
    sm = _sm()
    await sm.redis.sadd(sm.PIPELINE_ACTIVE_KEY, "t1")
    await sm.redis.set(sm.PIPELINE_CLAIM_KEY.format(task_id="t1"), "worker-a", ex=180)
    assert await sm.is_task_live("t1") is True
    # clear_stale_active must REFUSE while the claim exists (live worker).
    assert await sm.clear_stale_active("t1") is False
    assert await sm.is_task_active("t1") is True


@pytest.mark.asyncio
async def test_dead_worker_residue_is_not_live_and_clearable():
    sm = _sm()
    await sm.redis.sadd(sm.PIPELINE_ACTIVE_KEY, "t2")  # member, NO claim → hard-killed
    assert await sm.is_task_live("t2") is False
    assert await sm.clear_stale_active("t2") is True  # residue removed
    assert await sm.is_task_active("t2") is False  # reconciler can now resume it


@pytest.mark.asyncio
async def test_handoff_requeues_immediately_and_clears_claim():
    sm = _sm()
    await sm.redis.sadd(sm.PIPELINE_ACTIVE_KEY, "t3")
    await sm.redis.set(sm.PIPELINE_CLAIM_KEY.format(task_id="t3"), "worker-a")
    await sm.redis.lpush(sm.PIPELINE_PROCESSING_KEY, "t3")

    await sm.handoff_task("t3")

    # Queued for the next pod, claim cleared, still marked active (dedup).
    assert await sm.redis.lpos(sm.PIPELINE_QUEUE_KEY, "t3") is not None
    assert await sm.redis.exists(sm.PIPELINE_CLAIM_KEY.format(task_id="t3")) == 0
    assert await sm.is_task_active("t3") is True
    assert await sm.redis.lpos(sm.PIPELINE_PROCESSING_KEY, "t3") is None
    # And the handed-off task is NOT "live" — but it IS queued, which is the
    # exact state the reconciler must skip (a worker will claim it).
    assert await sm.is_task_live("t3") is False
