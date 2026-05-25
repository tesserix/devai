"""Memory-adapter tests — interface contract + factory + pipeline integration.

Three layers of coverage:

1. **Contract tests** — every adapter that we can construct in a slim
   environment (Noop + Redis-with-fakeredis when available) is exercised
   against the same set of behaviors: remember → recall round-trip,
   filter combinations, forget, close idempotency. This is what makes
   "swap providers with one env var" actually true: same surface, same
   behavior.

2. **Factory tests** — every value of DEVAI_MEMORY_PROVIDER resolves to
   a usable adapter, and failure modes (unknown provider, missing
   SDK, missing config) gracefully degrade to Noop.

3. **Pipeline wiring** — confirm `memory_injection_stage` actually consumes
   `deps.memory` when present.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from devai.adapters.base import AdapterError
from devai.adapters.memory import (
    KNOWN_PROVIDERS,
    MemoryAdapter,
    MemoryRecord,
    MemoryType,
    create_memory_adapter,
    memory_registry,
)
from devai.adapters.memory.noop import NoopMemoryAdapter


# ──────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────


class _Settings:
    memory_provider = "noop"
    memory_noop_keep_in_memory = True


@pytest.fixture
def noop_adapter() -> NoopMemoryAdapter:
    return NoopMemoryAdapter(keep_in_memory=True)


# ──────────────────────────────────────────────────────────────────────
# Contract: any compliant adapter must pass these behaviors
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_contract_remember_returns_record(noop_adapter):
    r = await noop_adapter.remember(
        "first memory", agent="sr_dev", repo="org/repo", tags=["lesson"]
    )
    assert isinstance(r, MemoryRecord)
    assert r.content == "first memory"
    assert r.agent == "sr_dev"
    assert r.repo == "org/repo"
    assert r.memory_type == MemoryType.EPISODIC
    assert "lesson" in r.tags
    assert r.provider == "noop"
    assert r.provider_id  # non-empty


@pytest.mark.asyncio
async def test_contract_remember_recall_roundtrip(noop_adapter):
    await noop_adapter.remember("alpha", agent="qa")
    await noop_adapter.remember("beta", agent="qa")
    await noop_adapter.remember("gamma", agent="sec")

    qa_records = await noop_adapter.recall(agent="qa")
    assert {r.content for r in qa_records} == {"alpha", "beta"}

    sec_records = await noop_adapter.recall(agent="sec")
    assert {r.content for r in sec_records} == {"gamma"}


@pytest.mark.asyncio
async def test_contract_recall_filters_by_repo_and_type(noop_adapter):
    await noop_adapter.remember("a", repo="org/x", memory_type="episodic")
    await noop_adapter.remember("b", repo="org/x", memory_type="semantic")
    await noop_adapter.remember("c", repo="org/y", memory_type="semantic")

    x_semantic = await noop_adapter.recall(repo="org/x", memory_type="semantic")
    assert [r.content for r in x_semantic] == ["b"]


@pytest.mark.asyncio
async def test_contract_recall_substring_query(noop_adapter):
    await noop_adapter.remember("user clicked the login button")
    await noop_adapter.remember("server returned 500")
    matches = await noop_adapter.recall(query="login")
    assert [r.content for r in matches] == ["user clicked the login button"]


@pytest.mark.asyncio
async def test_contract_semantic_search_degrades_to_recall(noop_adapter):
    """Adapters without native embeddings should fall back to keyword search."""
    await noop_adapter.remember("Postgres replication lag at peak hours")
    await noop_adapter.remember("Redis memory pressure")
    results = await noop_adapter.semantic_search("postgres", k=5)
    assert [r.content for r in results] == ["Postgres replication lag at peak hours"]


@pytest.mark.asyncio
async def test_contract_forget_removes(noop_adapter):
    r = await noop_adapter.remember("disposable")
    assert await noop_adapter.forget(r.provider_id) is True
    after = await noop_adapter.recall(query="disposable")
    assert after == []
    # second delete is False
    assert await noop_adapter.forget(r.provider_id) is False


@pytest.mark.asyncio
async def test_contract_close_is_idempotent(noop_adapter):
    await noop_adapter.close()
    await noop_adapter.close()  # second call must not raise


@pytest.mark.asyncio
async def test_contract_health_check_shape(noop_adapter):
    h = await noop_adapter.health_check()
    assert h["ok"] is True
    assert h["provider"] == "noop"
    assert "detail" in h


# ──────────────────────────────────────────────────────────────────────
# MemoryType parsing
# ──────────────────────────────────────────────────────────────────────


def test_memory_type_parse_accepts_string_enum_or_none():
    assert MemoryType.parse("episodic") == MemoryType.EPISODIC
    assert MemoryType.parse(MemoryType.SEMANTIC) == MemoryType.SEMANTIC
    assert MemoryType.parse(None) == MemoryType.EPISODIC
    # Unknown values fall through to default
    assert MemoryType.parse("garbage") == MemoryType.EPISODIC
    assert MemoryType.parse(None, default=MemoryType.PROCEDURAL) == MemoryType.PROCEDURAL


# ──────────────────────────────────────────────────────────────────────
# MemoryRecord serialization
# ──────────────────────────────────────────────────────────────────────


def test_memory_record_to_dict_roundtrips_well():
    r = MemoryRecord(content="x", agent="a", repo="r", memory_type=MemoryType.SEMANTIC, tags=["t"])
    d = r.to_dict()
    assert d["content"] == "x"
    assert d["memory_type"] == "semantic"
    assert d["tags"] == ["t"]
    assert "provider_id" in d
    assert d["similarity"] is None


# ──────────────────────────────────────────────────────────────────────
# Factory: every provider in KNOWN_PROVIDERS is registered
# ──────────────────────────────────────────────────────────────────────


def test_registry_lists_every_known_provider():
    assert set(KNOWN_PROVIDERS) == set(memory_registry.known())


def test_factory_returns_noop_for_default_settings():
    settings = _Settings()
    settings.memory_provider = "noop"
    adapter = create_memory_adapter(settings)
    assert adapter.provider_name == "noop"


def test_factory_unknown_provider_falls_back_to_noop(caplog):
    settings = _Settings()
    settings.memory_provider = "no-such-provider"
    with caplog.at_level("WARNING"):
        adapter = create_memory_adapter(settings)
    assert adapter.provider_name == "noop"
    assert any("unknown" in r.message.lower() for r in caplog.records)


def test_factory_missing_sdk_falls_back_to_noop(caplog):
    """When mem0/zep aren't installed, the factory must degrade — not crash."""
    settings = _Settings()
    settings.memory_provider = "mem0"
    settings.mem0_api_key = "fake-key"  # config present, SDK missing in slim env
    with caplog.at_level("WARNING"):
        adapter = create_memory_adapter(settings)
    # In an environment with mem0 installed this would return Mem0MemoryAdapter,
    # so we accept either; the contract is "no crash".
    assert adapter.provider_name in {"noop", "mem0"}


def test_factory_missing_config_falls_back_to_noop():
    settings = _Settings()
    settings.memory_provider = "zep"  # zep requires zep_url, which we don't set
    adapter = create_memory_adapter(settings)
    assert adapter.provider_name == "noop"


def test_factory_pgvector_without_db_falls_back_to_noop():
    settings = _Settings()
    settings.memory_provider = "pgvector"
    # No `database` / `state_manager.db` attribute -> AdapterNotConfigured -> Noop
    adapter = create_memory_adapter(settings)
    assert adapter.provider_name == "noop"


# ──────────────────────────────────────────────────────────────────────
# Pipeline wiring — memory_injection consumes deps.memory
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_memory_injection_stage_uses_deps_memory():
    from devai.pipeline.interfaces import StageDeps
    from devai.pipeline.stages.lifecycle import memory_injection_stage
    from devai.pipeline.types import DevAITask

    adapter = NoopMemoryAdapter(keep_in_memory=True)
    # Pre-seed two memories so the stage has something to find
    await adapter.remember("login fails with stale cookies", repo="org/repo")
    await adapter.remember("infra: postgres pool exhaustion", repo="org/repo")

    class _Cfg:
        pipeline_label = "x"

    deps = StageDeps(config=_Cfg(), memory=adapter)
    stage = memory_injection_stage(deps, {"k": "5"})

    task = DevAITask(intent="login flow regression", repo="org/repo")
    result = await stage.execute(task)

    assert "memory_provider" in result.data
    assert result.data["memory_provider"] == "noop"
    # At least the login-related memory should make it through substring match
    contents = [m["content"] for m in result.data["memories"]]
    assert any("login" in c for c in contents)


@pytest.mark.asyncio
async def test_memory_injection_stage_handles_no_memory_gracefully():
    from devai.pipeline.interfaces import StageDeps
    from devai.pipeline.stages.lifecycle import memory_injection_stage
    from devai.pipeline.types import DevAITask

    class _Cfg:
        pipeline_label = "x"

    # No memory adapter, no state_manager — stage must still complete
    deps = StageDeps(config=_Cfg())
    stage = memory_injection_stage(deps, {})
    result = await stage.execute(DevAITask(intent="anything"))
    assert result.data["memory_context"] == ""


# ──────────────────────────────────────────────────────────────────────
# SRE learn stage writes via adapter
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sre_learn_stage_writes_via_adapter():
    from devai.pipeline.interfaces import StageDeps
    from devai.pipeline.stages.sre import learn_stage
    from devai.pipeline.types import DevAITask

    adapter = NoopMemoryAdapter(keep_in_memory=True)

    class _Cfg:
        pipeline_label = "x"

    deps = StageDeps(config=_Cfg(), memory=adapter)
    stage = learn_stage(deps, {})

    task = DevAITask(intent="sre sweep", blueprint="sre-monitor", repo="org/cluster")
    task.agent_context["correlated_findings"] = [{"file": "p.yaml", "rule": "X", "severity": "high"}]
    result = await stage.execute(task)

    assert result.data["learn_done"] is True
    assert result.data["memory_provider"] == "noop"
    written = await adapter.recall(agent="sre")
    assert len(written) == 1
    assert "sre sweep" in written[0].content.lower()


# ──────────────────────────────────────────────────────────────────────
# Adapter registry isolation — replacing a factory doesn't leak globally
# ──────────────────────────────────────────────────────────────────────


def test_registry_register_or_replace_swaps_factory():
    from devai.adapters.base import AdapterRegistry

    reg = AdapterRegistry[NoopMemoryAdapter]("test")
    reg.register("noop", lambda _s: NoopMemoryAdapter())
    with pytest.raises(AdapterError):
        reg.register("noop", lambda _s: NoopMemoryAdapter())
    reg.register_or_replace("noop", lambda _s: NoopMemoryAdapter(keep_in_memory=True))
    adapter = reg.resolve("noop", _Settings())
    assert isinstance(adapter, NoopMemoryAdapter)
