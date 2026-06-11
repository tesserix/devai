"""Factory — pick the right memory backend at runtime.

Reads `settings.memory_provider` (env: `DEVAI_MEMORY_PROVIDER`) and returns
one of:

    noop      → NoopMemoryAdapter
    redis     → RedisMemoryAdapter
    pgvector  → PgVectorMemoryAdapter
    mem0      → Mem0MemoryAdapter
    zep       → ZepMemoryAdapter
    hondo     → HondoMemoryAdapter

Every successfully built backend is wrapped in `InstrumentedMemoryAdapter`
so memory ops emit count/latency/hit metrics into the global telemetry sink.
For pgvector, the factory also builds an embedder from the LLM adapter
family per `DEVAI_EMBEDDING_PROVIDER` (see `_build_embedder`) — without one,
semantic search degrades to keyword recall.

Graceful degradation rules:
  - If the selected provider's SDK isn't installed, factory falls back to
    NoopMemoryAdapter and logs a warning. The pod doesn't crash.
  - If the selected provider's required settings are missing, same fallback.
  - The provider value itself is not validated against an enum here — if
    the operator wrote `DEVAI_MEMORY_PROVIDER=zeep` (typo) we log + Noop.

The registry pattern means tests can swap in mock factories without
monkey-patching this module.
"""

from __future__ import annotations

import logging
from typing import Any

from devai.adapters.base import (
    AdapterError,
    AdapterNotConfigured,
    AdapterNotInstalled,
    AdapterRegistry,
)
from devai.adapters.memory.base import MemoryAdapter
from devai.adapters.memory.noop import NoopMemoryAdapter

logger = logging.getLogger(__name__)

KNOWN_PROVIDERS = ("noop", "redis", "pgvector", "mem0", "zep", "hondo")


def _build_noop(settings: Any) -> MemoryAdapter:
    # Read an optional dict-style flag from settings for tests
    keep = bool(getattr(settings, "memory_noop_keep_in_memory", False))
    return NoopMemoryAdapter(keep_in_memory=keep)


def _build_redis(settings: Any) -> MemoryAdapter:
    from devai.adapters.memory.redis_adapter import RedisMemoryAdapter

    redis_client = _resolve_redis(settings)
    if redis_client is None:
        raise AdapterNotConfigured("redis memory adapter requires a redis client")
    return RedisMemoryAdapter(redis_client)


def _build_pgvector(settings: Any) -> MemoryAdapter:
    from devai.adapters.memory.pgvector_adapter import PgVectorMemoryAdapter

    db = _resolve_database(settings)
    if db is None:
        raise AdapterNotConfigured("pgvector memory adapter requires a connected Database")
    # An explicitly attached embedder (tests, custom wiring) wins; otherwise
    # build one from the LLM adapter family per DEVAI_EMBEDDING_PROVIDER.
    embedder = getattr(settings, "memory_embedder", None) or _build_embedder(settings)
    return PgVectorMemoryAdapter(db, embedder=embedder)


def _build_mem0(settings: Any) -> MemoryAdapter:
    from devai.adapters.memory.mem0_adapter import Mem0MemoryAdapter

    api_key = getattr(settings, "mem0_api_key", "") or ""
    host = getattr(settings, "mem0_host", "") or ""
    if not api_key and not host:
        raise AdapterNotConfigured("mem0 adapter requires DEVAI_MEM0_API_KEY or DEVAI_MEM0_HOST")
    return Mem0MemoryAdapter(api_key=api_key, host=host)


def _build_zep(settings: Any) -> MemoryAdapter:
    from devai.adapters.memory.zep_adapter import ZepMemoryAdapter

    url = getattr(settings, "zep_url", "") or ""
    api_key = getattr(settings, "zep_api_key", "") or ""
    if not url:
        raise AdapterNotConfigured("zep adapter requires DEVAI_ZEP_URL")
    return ZepMemoryAdapter(url=url, api_key=api_key)


def _build_hondo(settings: Any) -> MemoryAdapter:
    from devai.adapters.memory.hondo_adapter import HondoMemoryAdapter

    url = getattr(settings, "hondo_url", "") or ""
    api_key = getattr(settings, "hondo_api_key", "") or ""
    if not url and not api_key:
        raise AdapterNotConfigured("hondo adapter requires DEVAI_HONDO_URL or DEVAI_HONDO_API_KEY")
    return HondoMemoryAdapter(url=url, api_key=api_key)


def _build_embedder(settings: Any) -> Any | None:
    """Build an `embed(text)` object from the LLM adapter family.

    Controlled by `DEVAI_EMBEDDING_PROVIDER`:
        auto    → use OpenAI when DEVAI_OPENAI_API_KEY is set, else None
        openai  → require the OpenAI key, warn + None when missing
        none    → explicitly disabled

    Never raises — without an embedder pgvector degrades to keyword recall,
    which is the documented fallback, not an error.
    """
    provider = (getattr(settings, "embedding_provider", "auto") or "auto").lower()
    if provider in ("none", "off", "noop", "disabled"):
        return None

    api_key = getattr(settings, "openai_api_key", "") or ""
    if not api_key:
        if provider == "openai":
            logger.warning(
                "embedding_provider=openai but DEVAI_OPENAI_API_KEY is unset — semantic search degrades to keyword recall"
            )
        else:
            logger.info(
                "embedding_provider=auto with no OpenAI key — semantic search degrades to keyword recall"
            )
        return None

    try:
        from devai.adapters.llm.openai_adapter import OpenAILLMAdapter
        from devai.adapters.memory.embedder import LLMEmbedder

        llm = OpenAILLMAdapter(
            api_key=api_key,
            base_url=getattr(settings, "openai_base_url", "") or "",
            organization=getattr(settings, "openai_organization", "") or "",
        )
        model = getattr(settings, "embedding_model", "") or "text-embedding-3-small"
        dimensions = int(getattr(settings, "embedding_dimensions", 0) or 0)
        logger.info("memory embedder active: openai model=%s dims=%s", model, dimensions or "unchecked")
        return LLMEmbedder(llm, model=model, dimensions=dimensions)
    except (AdapterNotInstalled, AdapterNotConfigured) as e:
        logger.warning("memory embedder unavailable (%s) — semantic search degrades to keyword recall", e)
        return None
    except Exception:  # noqa: BLE001
        logger.exception("memory embedder construction crashed — semantic search degrades to keyword recall")
        return None


memory_registry: AdapterRegistry[MemoryAdapter] = AdapterRegistry("memory")
memory_registry.register("noop", _build_noop)
memory_registry.register("redis", _build_redis)
memory_registry.register("pgvector", _build_pgvector)
memory_registry.register("mem0", _build_mem0)
memory_registry.register("zep", _build_zep)
memory_registry.register("hondo", _build_hondo)


def create_memory_adapter(settings: Any) -> MemoryAdapter:
    """Resolve `settings.memory_provider` to a constructed adapter.

    Never raises — on any error (unknown provider, missing SDK, missing
    config) returns a NoopMemoryAdapter and logs the reason.
    """
    provider = (getattr(settings, "memory_provider", "noop") or "noop").lower()
    if not memory_registry.has(provider):
        logger.warning(
            "memory_provider=%r is unknown (known: %s) — using Noop",
            provider,
            ", ".join(memory_registry.known()),
        )
        return NoopMemoryAdapter()

    try:
        adapter = memory_registry.resolve(provider, settings)
        logger.info("MemoryAdapter active: %s", adapter.provider_name)
        # Wrap every backend in the telemetry delegate so EVERY caller —
        # memory_injection, learn stages, chat tools — emits per-op
        # count/latency/hit metrics. Free when telemetry is Noop.
        from devai.adapters.memory.instrumented import InstrumentedMemoryAdapter

        return InstrumentedMemoryAdapter(adapter)
    except AdapterNotInstalled as e:
        logger.warning("memory_provider=%s: %s — using Noop", provider, e)
        return NoopMemoryAdapter()
    except AdapterNotConfigured as e:
        logger.warning("memory_provider=%s: %s — using Noop", provider, e)
        return NoopMemoryAdapter()
    except AdapterError as e:
        logger.warning("memory_provider=%s failed to build (%s) — using Noop", provider, e)
        return NoopMemoryAdapter()
    except Exception:  # noqa: BLE001
        logger.exception("memory_provider=%s crashed during build — using Noop", provider)
        return NoopMemoryAdapter()


# ──────────────────────────────────────────────────────────────────────
# Convenience resolvers — turn settings into concrete dep objects
# ──────────────────────────────────────────────────────────────────────


def _resolve_redis(settings: Any) -> Any | None:
    """Return a redis.asyncio.Redis client, constructing one if needed.

    Order of preference:
      1. `settings.redis_client` if directly attached (used by tests)
      2. `settings.state_manager.redis` if a StateManager is attached
      3. Construct from `settings.redis_url`
    """
    client = getattr(settings, "redis_client", None)
    if client is not None:
        return client
    sm = getattr(settings, "state_manager", None)
    if sm is not None and hasattr(sm, "redis"):
        return sm.redis
    url = getattr(settings, "redis_url", None)
    if not url:
        return None
    try:
        import redis.asyncio as redis

        return redis.from_url(url, decode_responses=True)
    except Exception:  # noqa: BLE001
        logger.exception("redis client construction failed")
        return None


def _resolve_database(settings: Any) -> Any | None:
    """Return a connected Database instance.

    Order of preference:
      1. `settings.database` if directly attached
      2. `settings.state_manager.db` if attached
      3. Return None — pgvector adapter raises AdapterNotConfigured

    Note: this resolver does NOT construct + connect a new Database; that
    would block the factory on I/O. PgVector adapter consumers are expected
    to attach the already-connected Database to settings before calling.
    """
    db = getattr(settings, "database", None)
    if db is not None:
        return db
    sm = getattr(settings, "state_manager", None)
    if sm is not None and hasattr(sm, "db"):
        return sm.db
    return None


__all__ = ["KNOWN_PROVIDERS", "create_memory_adapter", "memory_registry"]
