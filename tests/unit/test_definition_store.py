"""Direct tests for the DefinitionStore backends (in-memory + Redis)."""

from __future__ import annotations

import pytest

from devai.authoring.store import (
    AuthoredDefinition,
    InMemoryDefinitionStore,
    RedisDefinitionStore,
)


def _defn(kind: str, name: str, yaml: str = "x: 1") -> AuthoredDefinition:
    return AuthoredDefinition(kind=kind, name=name, yaml=yaml, created_by="tester")


async def _make_redis_store():
    fakeredis = pytest.importorskip("fakeredis")
    return RedisDefinitionStore(fakeredis.aioredis.FakeRedis())


# Run every test against both backends so they stay behaviourally identical.
@pytest.fixture(params=["memory", "redis"])
async def store(request):
    if request.param == "memory":
        return InMemoryDefinitionStore()
    return await _make_redis_store()


async def test_upsert_then_get(store):
    await store.upsert(_defn("specialization", "a"))
    got = await store.get("specialization", "a")
    assert got is not None
    assert got.name == "a"
    assert got.created_by == "tester"
    assert got.created_at and got.updated_at


async def test_get_missing_returns_none(store):
    assert await store.get("specialization", "nope") is None


async def test_list_is_isolated_by_kind(store):
    await store.upsert(_defn("specialization", "agent1"))
    await store.upsert(_defn("specialization", "agent2"))
    await store.upsert(_defn("blueprint", "flow1"))

    specs = sorted(d.name for d in await store.list("specialization"))
    blueprints = [d.name for d in await store.list("blueprint")]
    assert specs == ["agent1", "agent2"]
    assert blueprints == ["flow1"]


async def test_upsert_updates_in_place(store):
    await store.upsert(_defn("specialization", "a", yaml="v: 1"))
    await store.upsert(_defn("specialization", "a", yaml="v: 2"))
    rows = await store.list("specialization")
    assert len(rows) == 1
    assert (await store.get("specialization", "a")).yaml == "v: 2"


async def test_delete_removes_and_clears_index(store):
    await store.upsert(_defn("specialization", "a"))
    assert await store.delete("specialization", "a") is True
    assert await store.get("specialization", "a") is None
    # Gone from the listing (index cleaned up), not a dangling entry.
    assert await store.list("specialization") == []
    # Second delete is a no-op.
    assert await store.delete("specialization", "a") is False
