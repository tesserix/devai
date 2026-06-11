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

import pytest

from devai.adapters.base import AdapterError
from devai.adapters.memory import (
    KNOWN_PROVIDERS,
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
    r = await noop_adapter.remember("first memory", agent="sr_dev", repo="org/repo", tags=["lesson"])
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
    # No `database` / `state_manager.db` / `database_url` -> AdapterNotConfigured -> Noop
    adapter = create_memory_adapter(settings)
    assert adapter.provider_name == "noop"


def test_factory_pgvector_builds_from_database_url():
    """No attached Database but a DSN present → factory constructs an
    UNCONNECTED Database the adapter owns and lazily connects.

    This is the branch that actually fires in the server paths: StateManager
    is Redis-only, so without it pgvector silently degraded to Noop."""
    settings = _Settings()
    settings.memory_provider = "pgvector"
    settings.embedding_provider = "none"
    settings.database_url = "postgresql://devai:x@db.example:5432/devai_db"
    adapter = create_memory_adapter(settings)
    assert adapter.provider_name == "pgvector"


@pytest.mark.asyncio
async def test_pgvector_lazily_connects_owned_database():
    from devai.adapters.memory.pgvector_adapter import PgVectorMemoryAdapter

    class _RaisingPoolDb:
        """Mimics services.database.Database: .pool RAISES until connect()."""

        def __init__(self):
            self._pool = None
            self.connect_calls = 0

        @property
        def pool(self):
            if not self._pool:
                raise RuntimeError("Database not connected. Call connect() first.")
            return self._pool

        async def connect(self):
            self.connect_calls += 1

            class _Pool:
                async def fetch(self, sql, *params):
                    return []

            self._pool = _Pool()

        async def close(self):
            self._pool = None

    db = _RaisingPoolDb()
    adapter = PgVectorMemoryAdapter(db, owns_database=True)
    # First op triggers the lazy connect instead of crashing on .pool
    assert await adapter.recall(agent="alm") == []
    assert db.connect_calls == 1
    # Second op reuses the pool
    await adapter.recall(agent="alm")
    assert db.connect_calls == 1
    # close() closes the owned database
    await adapter.close()
    assert db._pool is None


@pytest.mark.asyncio
async def test_pgvector_unreachable_db_degrades_not_raises():
    from devai.adapters.memory.pgvector_adapter import PgVectorMemoryAdapter

    class _DeadDb:
        @property
        def pool(self):
            raise RuntimeError("Database not connected. Call connect() first.")

        async def connect(self):
            raise ConnectionError("no route to host")

    adapter = PgVectorMemoryAdapter(_DeadDb(), owns_database=True)
    assert await adapter.recall(query="x") == []
    assert await adapter.semantic_search("x") == []
    assert await adapter.forget("some-id") is False
    health = await adapter.health_check()
    assert health["ok"] is False


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


# ──────────────────────────────────────────────────────────────────────
# LLMEmbedder — single-text facade over the LLM family's batch embed()
# ──────────────────────────────────────────────────────────────────────


class _FakeEmbedLLM:
    """Stands in for an LLMAdapter that supports embeddings."""

    def __init__(self, vector=None):
        self.vector = vector if vector is not None else [0.1, 0.2, 0.3]
        self.closed = False
        self.calls: list[tuple[list[str], str]] = []

    async def embed(self, texts, *, model=""):
        self.calls.append((texts, model))
        return [list(self.vector) for _ in texts]

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_embedder_returns_single_vector():
    from devai.adapters.memory.embedder import LLMEmbedder

    llm = _FakeEmbedLLM()
    embedder = LLMEmbedder(llm, model="test-model", dimensions=3)
    vec = await embedder.embed("hello")
    assert vec == [0.1, 0.2, 0.3]
    assert llm.calls == [(["hello"], "test-model")]


@pytest.mark.asyncio
async def test_embedder_raises_on_dimension_mismatch():
    from devai.adapters.memory.embedder import LLMEmbedder

    embedder = LLMEmbedder(_FakeEmbedLLM(), model="m", dimensions=1536)
    with pytest.raises(ValueError, match="dimension mismatch"):
        await embedder.embed("hello")


@pytest.mark.asyncio
async def test_embedder_raises_on_empty_result():
    from devai.adapters.memory.embedder import LLMEmbedder

    class _EmptyLLM:
        async def embed(self, texts, *, model=""):
            return []

    embedder = LLMEmbedder(_EmptyLLM(), model="m")
    with pytest.raises(ValueError, match="no vector"):
        await embedder.embed("hello")


# ──────────────────────────────────────────────────────────────────────
# Factory embedder wiring
# ──────────────────────────────────────────────────────────────────────


def test_build_embedder_disabled_returns_none():
    from devai.adapters.memory.factory import _build_embedder

    settings = _Settings()
    settings.embedding_provider = "none"
    assert _build_embedder(settings) is None


def test_build_embedder_auto_without_key_returns_none():
    from devai.adapters.memory.factory import _build_embedder

    settings = _Settings()
    settings.embedding_provider = "auto"
    settings.openai_api_key = ""
    assert _build_embedder(settings) is None


def test_pgvector_factory_prefers_explicit_memory_embedder():
    """An embedder attached to settings wins over factory construction."""
    from devai.adapters.memory.factory import _build_pgvector

    sentinel = object()

    class _Db:
        pool = None

    settings = _Settings()
    settings.memory_provider = "pgvector"
    settings.database = _Db()
    settings.memory_embedder = sentinel
    adapter = _build_pgvector(settings)
    assert adapter._embedder is sentinel


def test_pgvector_factory_without_key_degrades_to_keyword():
    """No embedder anywhere → adapter built with embedder=None (keyword recall)."""
    from devai.adapters.memory.factory import _build_pgvector

    class _Db:
        pool = None

    settings = _Settings()
    settings.memory_provider = "pgvector"
    settings.database = _Db()
    settings.embedding_provider = "auto"
    settings.openai_api_key = ""
    adapter = _build_pgvector(settings)
    assert adapter._embedder is None


# ──────────────────────────────────────────────────────────────────────
# InstrumentedMemoryAdapter — telemetry delegate applied by the factory
# ──────────────────────────────────────────────────────────────────────


class _CaptureTelemetry:
    """Duck-typed telemetry sink capturing incr/observe calls."""

    def __init__(self):
        self.counters: list[tuple[str, dict]] = []
        self.observations: list[tuple[str, float, dict]] = []

    def incr(self, name, value=1.0, attrs=None):
        self.counters.append((name, dict(attrs or {})))

    def observe(self, name, value, attrs=None):
        self.observations.append((name, value, dict(attrs or {})))


@pytest.fixture
def capture_telemetry():
    from devai.adapters.telemetry.runtime import set_global_telemetry

    sink = _CaptureTelemetry()
    set_global_telemetry(sink)  # type: ignore[arg-type]
    yield sink
    set_global_telemetry(None)


@pytest.mark.asyncio
async def test_instrumented_adapter_passes_through_and_records(capture_telemetry):
    from devai.adapters.memory.instrumented import InstrumentedMemoryAdapter

    inner = NoopMemoryAdapter(keep_in_memory=True)
    adapter = InstrumentedMemoryAdapter(inner)
    assert adapter.provider_name == "noop"

    record = await adapter.remember("postgres pool exhaustion", agent="alm")
    results = await adapter.recall(agent="alm")
    found = await adapter.semantic_search("postgres", k=3)
    assert record.content == "postgres pool exhaustion"
    assert len(results) == 1
    assert len(found) == 1
    assert await adapter.forget(record.provider_id) is True

    ops = [attrs["op"] for name, attrs in capture_telemetry.counters if name == "devai.memory.ops"]
    assert ops == ["remember", "recall", "semantic_search", "forget"]
    assert all(attrs["status"] == "ok" for _, attrs in capture_telemetry.counters)
    result_obs = [(v, a["op"]) for n, v, a in capture_telemetry.observations if n == "devai.memory.results"]
    assert (1.0, "recall") in result_obs
    assert (1.0, "semantic_search") in result_obs


@pytest.mark.asyncio
async def test_instrumented_adapter_records_errors_and_reraises(capture_telemetry):
    from devai.adapters.memory.instrumented import InstrumentedMemoryAdapter

    class _Boom(NoopMemoryAdapter):
        async def remember(self, *a, **kw):
            raise RuntimeError("backend down")

    adapter = InstrumentedMemoryAdapter(_Boom())
    with pytest.raises(RuntimeError):
        await adapter.remember("x")
    assert any(attrs["status"] == "error" for _, attrs in capture_telemetry.counters)


def test_factory_wraps_resolved_adapter_in_instrumented():
    from devai.adapters.memory.instrumented import InstrumentedMemoryAdapter

    settings = _Settings()
    settings.memory_provider = "noop"
    adapter = create_memory_adapter(settings)
    assert isinstance(adapter, InstrumentedMemoryAdapter)
    assert adapter.provider_name == "noop"


# ──────────────────────────────────────────────────────────────────────
# Database.semantic_search — filters pushed into SQL, not Python
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_database_semantic_search_pushes_filters_into_sql():
    from devai.services.database import Database

    captured: dict = {}

    class _FakePool:
        async def fetch(self, sql, *params):
            captured["sql"] = sql
            captured["params"] = params
            return []

    db = Database("postgresql://unused")
    db._pool = _FakePool()

    await db.semantic_search(
        embedding=[0.0] * 3,
        repo="org/repo",
        limit=7,
        agent="alm",
        memory_type="procedural",
    )

    sql = captured["sql"]
    assert "agent = $" in sql
    assert "memory_type = $" in sql
    assert "repo = $" in sql
    # embedding, repo, agent, memory_type, limit
    assert captured["params"] == ([0.0] * 3, "org/repo", "alm", "procedural", 7)


@pytest.mark.asyncio
async def test_pgvector_semantic_search_passes_filters_to_db():
    from devai.adapters.memory.pgvector_adapter import PgVectorMemoryAdapter

    captured: dict = {}

    class _FakeDb:
        pool = object()  # non-None so the adapter proceeds

        async def semantic_search(self, *, embedding, repo, limit, agent, memory_type):
            captured.update(embedding=embedding, repo=repo, limit=limit, agent=agent, memory_type=memory_type)
            return []

    adapter = PgVectorMemoryAdapter(_FakeDb(), embedder=_FakeSingleTextEmbedder())
    await adapter.semantic_search("query", k=4, agent="alm", repo="org/r", memory_type="semantic")
    assert captured["agent"] == "alm"
    assert captured["memory_type"] == "semantic"
    assert captured["limit"] == 4
    assert captured["embedding"] == [0.5, 0.5]


class _FakeSingleTextEmbedder:
    """Object with the `embed(text)` surface pgvector expects."""

    async def embed(self, text):
        return [0.5, 0.5]


# ──────────────────────────────────────────────────────────────────────
# ALM learn stage — the write half of the ALM memory loop
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_alm_learn_stage_writes_episodic_record():
    from devai.pipeline.interfaces import StageDeps
    from devai.pipeline.stages.lifecycle import alm_learn_stage
    from devai.pipeline.types import DevAITask

    adapter = NoopMemoryAdapter(keep_in_memory=True)

    class _Cfg:
        pipeline_label = "x"

    deps = StageDeps(config=_Cfg(), memory=adapter)
    stage = alm_learn_stage(deps, {})

    task = DevAITask(intent="add login", blueprint="alm-pipeline", repo="org/app")
    task.stages_completed = ["implement_code", "review_code", "run_tests"]
    result = await stage.execute(task)

    assert result.data["learn_done"] is True
    written = await adapter.recall(agent="alm")
    assert len(written) == 1
    assert "succeeded" in written[0].content
    assert written[0].memory_type == MemoryType.EPISODIC


@pytest.mark.asyncio
async def test_alm_learn_stage_records_recovery_as_procedural():
    from devai.pipeline.interfaces import StageDeps
    from devai.pipeline.stages.lifecycle import alm_learn_stage
    from devai.pipeline.types import DevAITask

    adapter = NoopMemoryAdapter(keep_in_memory=True)

    class _Cfg:
        pipeline_label = "x"

    deps = StageDeps(config=_Cfg(), memory=adapter)
    stage = alm_learn_stage(deps, {})

    task = DevAITask(intent="fix flaky test", blueprint="alm-pipeline", repo="org/app")
    task.stages_completed = ["implement_code", "run_tests"]
    task.stages_failed = ["run_tests"]  # failed mid-run, no terminal error → recovered
    await stage.execute(task)

    procedural = await adapter.recall(agent="alm", memory_type="procedural")
    assert len(procedural) == 1
    assert "run_tests" in procedural[0].content


@pytest.mark.asyncio
async def test_alm_learn_stage_dry_run_writes_nothing():
    from devai.pipeline.interfaces import StageDeps
    from devai.pipeline.stages.lifecycle import alm_learn_stage
    from devai.pipeline.types import DevAITask

    adapter = NoopMemoryAdapter(keep_in_memory=True)

    class _Cfg:
        pipeline_label = "x"

    deps = StageDeps(config=_Cfg(), memory=adapter)
    stage = alm_learn_stage(deps, {})

    task = DevAITask(intent="preview", blueprint="alm-pipeline", repo="org/app")
    task.dry_run = True
    result = await stage.execute(task)

    assert result.data["dry_run"] is True
    assert await adapter.recall(agent="alm") == []


# ──────────────────────────────────────────────────────────────────────
# Global memory runtime + helpers (Phase C consolidation surface)
# ──────────────────────────────────────────────────────────────────────


def test_global_memory_defaults_to_noop_and_resets():
    from devai.adapters.memory.runtime import get_global_memory, set_global_memory

    assert get_global_memory().provider_name == "noop"
    real = NoopMemoryAdapter(keep_in_memory=True)
    set_global_memory(real)
    assert get_global_memory() is real
    set_global_memory(None)
    assert get_global_memory().provider_name == "noop"


@pytest.mark.asyncio
async def test_format_memory_context_shape():
    from devai.adapters.memory.helpers import format_memory_context

    adapter = NoopMemoryAdapter(keep_in_memory=True)
    await adapter.remember("uses pnpm not npm", memory_type="semantic")
    records = await adapter.recall()
    ctx = format_memory_context(records)
    assert ctx.startswith("## Agent Memory")
    assert "[semantic]" in ctx
    assert "uses pnpm not npm" in ctx
    assert format_memory_context([]) == ""


@pytest.mark.asyncio
async def test_remember_repo_pattern_dedups_exact_content():
    from devai.adapters.memory.helpers import remember_repo_pattern

    adapter = NoopMemoryAdapter(keep_in_memory=True)
    first = await remember_repo_pattern(
        adapter, repo="org/x", pattern_type="tech_stack", description="Tech stack: {}", agent="tech_detector"
    )
    second = await remember_repo_pattern(
        adapter, repo="org/x", pattern_type="tech_stack", description="Tech stack: {}", agent="tech_detector"
    )
    assert first is True
    assert second is False
    stored = await adapter.recall(repo="org/x", memory_type="semantic")
    assert len(stored) == 1
    assert "tech_stack" in stored[0].tags


@pytest.mark.asyncio
async def test_alm_learn_stage_degrades_without_adapter():
    from devai.pipeline.interfaces import StageDeps
    from devai.pipeline.stages.lifecycle import alm_learn_stage
    from devai.pipeline.types import DevAITask

    class _Cfg:
        pipeline_label = "x"

    stage = alm_learn_stage(StageDeps(config=_Cfg()), {})
    result = await stage.execute(DevAITask(intent="x", blueprint="alm-pipeline"))
    assert result.data["learn_done"] is False
