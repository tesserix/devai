"""Tests for the authoring service, store, and live registration."""

from __future__ import annotations

import pytest

from devai.authoring import create_authoring_service
from devai.authoring.service import AuthoringError
from devai.authoring.store import InMemoryDefinitionStore, RedisDefinitionStore
from devai.specializations.registry import SpecializationRegistry

_GOOD_FIELDS = {
    "name": "my_helper",
    "display_name": "My Helper",
    "category": "specialist",
    "llm_provider": "anthropic",
    "system_prompt": "You help.",
    "allowed_tools": ["scm_list_files"],
    "handover_schema": {"answer": {"type": "string", "required": True}},
}

_GOOD_BLUEPRINT = """
name: my-flow
description: a tiny authored flow
stages:
  - name: step-1
    stage: run_specialization
    config:
      specialization: my_helper
"""


async def test_create_agent_from_fields_persists_and_registers():
    registry = SpecializationRegistry()
    svc = create_authoring_service(redis=None, spec_registry=registry)

    out = await svc.create_specialization_from_fields(_GOOD_FIELDS, created_by="alice@corp.com")
    assert out["name"] == "my_helper"

    # Persisted...
    rows = await svc.list_specializations()
    assert any(r["name"] == "my_helper" and r["created_by"] == "alice@corp.com" for r in rows)
    # ...and registered live so a blueprint can resolve it immediately.
    assert registry.has("my_helper")
    assert registry.resolve("my_helper").system_prompt.strip() == "You help."


async def test_invalid_agent_rejected():
    svc = create_authoring_service(redis=None)
    with pytest.raises(AuthoringError):
        # Missing required `name` → loader rejects.
        await svc.create_specialization("description: no name here\n")


async def test_create_and_delete_blueprint():
    svc = create_authoring_service(redis=None)
    out = await svc.create_blueprint(_GOOD_BLUEPRINT, created_by="bob")
    assert out["name"] == "my-flow"
    assert out["stages"] == 1
    assert await svc.delete_blueprint("my-flow") is True
    assert await svc.get_blueprint("my-flow") is None


async def test_load_into_registry_rehydrates_stored_agents():
    store = InMemoryDefinitionStore()
    reg1 = SpecializationRegistry()
    svc1 = create_authoring_service(spec_registry=reg1)
    svc1._store = store  # share the store across "restarts"
    await svc1.create_specialization_from_fields(_GOOD_FIELDS)

    # Simulate a fresh process: new registry, same store, boot reload.
    reg2 = SpecializationRegistry()
    from devai.authoring.service import AuthoringService

    svc2 = AuthoringService(store, spec_registry=reg2)
    n = await svc2.load_into_registry()
    assert n == 1 and reg2.has("my_helper")


async def test_redis_store_roundtrip():
    fakeredis = pytest.importorskip("fakeredis")
    redis = fakeredis.aioredis.FakeRedis()
    store = RedisDefinitionStore(redis)
    svc = create_authoring_service(spec_registry=SpecializationRegistry())
    svc._store = store

    await svc.create_specialization_from_fields(_GOOD_FIELDS)
    rows = await svc.list_specializations()
    assert [r["name"] for r in rows] == ["my_helper"]
    assert await svc.delete_specialization("my_helper") is True
    assert await svc.list_specializations() == []
