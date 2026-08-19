"""The trace spine: what a sandbox invocation did, step by step.

Traces live as long as the sandbox that produced them and no longer — a pinned
configuration and its evidence expire together.
"""

from __future__ import annotations

import json

import pytest

from devai.adapters.object_store.noop import NoopObjectStoreAdapter
from devai.sandbox.trace import Invocation, TraceStep, TraceStore


class _FakeRedis:
    """Only the four calls the store makes."""

    def __init__(self) -> None:
        self.kv: dict[str, str] = {}
        self.lists: dict[str, list[str]] = {}
        self.expiries: dict[str, int] = {}

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.kv[key] = value
        if ex:
            self.expiries[key] = ex

    async def get(self, key: str) -> str | None:
        return self.kv.get(key)

    async def lpush(self, key: str, value: str) -> None:
        self.lists.setdefault(key, []).insert(0, value)

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        return self.lists.get(key, [])[start : (None if end == -1 else end + 1)]

    async def expire(self, key: str, seconds: int) -> None:
        self.expiries[key] = seconds


def _invocation(**kw) -> Invocation:
    return Invocation(
        id=kw.get("id", "inv-1"),
        sandbox_id=kw.get("sandbox_id", "sb-1"),
        agent=kw.get("agent", "release_notes_writer"),
        message=kw.get("message", "summarise the diff"),
        final_text=kw.get("final_text", "Here are the notes."),
        ok=kw.get("ok", True),
        steps=kw.get(
            "steps",
            [
                TraceStep(kind="prompt", name="system", output="You write release notes."),
                TraceStep(kind="llm", name="claude-sonnet-4", prompt_tokens=120, completion_tokens=40, latency_ms=900),
                TraceStep(kind="tool", name="read_file", mode="mock", latency_ms=3),
                TraceStep(kind="response", name="final", output="Here are the notes."),
            ],
        ),
    )


def test_totals_are_derived_from_the_steps() -> None:
    inv = _invocation()

    assert inv.totals["prompt_tokens"] == 120
    assert inv.totals["completion_tokens"] == 40
    assert inv.totals["total_tokens"] == 160
    assert inv.totals["tool_calls"] == 1
    assert inv.totals["llm_calls"] == 1
    assert inv.totals["latency_ms"] == 903
    assert inv.totals["wall_clock_ms"] == inv.wall_clock_ms


def test_cost_totals_when_the_provider_reports_it() -> None:
    inv = _invocation(
        steps=[
            TraceStep(kind="llm", name="a", cost_usd=0.012),
            TraceStep(kind="llm", name="b", cost_usd=0.009),
        ]
    )

    assert inv.totals["cost_usd"] == pytest.approx(0.021)


async def test_an_invocation_is_readable_after_it_is_stored() -> None:
    store = TraceStore(_FakeRedis())
    await store.save(_invocation(), ttl_seconds=3600)

    got = await store.get("sb-1", "inv-1")

    assert got is not None
    assert got.final_text == "Here are the notes."
    assert [s.kind for s in got.steps] == ["prompt", "llm", "tool", "response"]


async def test_invocations_list_newest_first() -> None:
    store = TraceStore(_FakeRedis())
    await store.save(_invocation(id="inv-1"), ttl_seconds=60)
    await store.save(_invocation(id="inv-2"), ttl_seconds=60)

    assert [i.id for i in await store.list_for_sandbox("sb-1")] == ["inv-2", "inv-1"]


async def test_a_trace_expires_with_its_sandbox() -> None:
    redis = _FakeRedis()
    store = TraceStore(redis)

    await store.save(_invocation(), ttl_seconds=1800)

    assert redis.expiries["devai:sandbox:sb-1:invocation:inv-1"] == 1800
    assert redis.expiries["devai:sandbox:sb-1:invocations"] == 1800


async def test_another_sandbox_cannot_read_the_trace() -> None:
    store = TraceStore(_FakeRedis())
    await store.save(_invocation(), ttl_seconds=60)

    assert await store.get("sb-other", "inv-1") is None


async def test_without_redis_the_store_still_works_in_process() -> None:
    store = TraceStore(None)
    await store.save(_invocation(), ttl_seconds=60)

    assert (await store.get("sb-1", "inv-1")) is not None
    assert len(await store.list_for_sandbox("sb-1")) == 1


async def test_a_corrupt_record_is_skipped_rather_than_raising() -> None:
    redis = _FakeRedis()
    store = TraceStore(redis)
    await store.save(_invocation(), ttl_seconds=60)
    redis.kv["devai:sandbox:sb-1:invocation:inv-1"] = "{not json"

    assert await store.get("sb-1", "inv-1") is None
    assert await store.list_for_sandbox("sb-1") == []


async def test_the_stored_shape_is_json_the_dashboard_can_read() -> None:
    redis = _FakeRedis()
    await TraceStore(redis).save(_invocation(), ttl_seconds=60)

    body = json.loads(redis.kv["devai:sandbox:sb-1:invocation:inv-1"])

    assert body["agent"] == "release_notes_writer"
    assert body["totals"]["total_tokens"] == 160
    assert body["steps"][2]["mode"] == "mock"


async def test_large_trace_payloads_are_offloaded_to_the_object_store() -> None:
    class _DurableObjectStore(NoopObjectStoreAdapter):
        provider_name = "durable-test"

    redis = _FakeRedis()
    objects = _DurableObjectStore()
    store = TraceStore(redis, object_store=objects, inline_limit_bytes=32)

    await store.save(_invocation(message="x" * 500), ttl_seconds=60)

    metadata = json.loads(redis.kv["devai:sandbox:sb-1:invocation:inv-1"])
    assert set(metadata) == {"object_key"}
    assert metadata["object_key"] == "sandbox-traces/sb-1/inv-1.json"
    assert json.loads((await objects.get(metadata["object_key"])).decode())["message"] == "x" * 500
    assert (await store.get("sb-1", "inv-1")).message == "x" * 500


async def test_ephemeral_object_store_never_replaces_cross_replica_redis_storage() -> None:
    redis = _FakeRedis()
    store = TraceStore(redis, object_store=NoopObjectStoreAdapter(), inline_limit_bytes=32)

    await store.save(_invocation(message="x" * 500), ttl_seconds=60)

    stored = json.loads(redis.kv["devai:sandbox:sb-1:invocation:inv-1"])
    assert stored["id"] == "inv-1"
    assert "object_key" not in stored
