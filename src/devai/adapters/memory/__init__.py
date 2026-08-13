"""Memory adapter family — pluggable Agentic AI memory backends.

Public surface:

    MemoryAdapter        — the ABC every backend implements
    MemoryRecord         — canonical record returned by every adapter
    MemoryType           — episodic | semantic | procedural
    create_memory_adapter(settings) — factory; reads DEVAI_MEMORY_PROVIDER

Backends:

    noop       — empty/in-memory; for tests and disabled mode
    redis      — wraps existing devai.services.memory.AgentMemory (backward compat)
    pgvector   — Postgres + pgvector embeddings (production default for DevAI)
    qdrant     — Qdrant vector store over its REST API (needs an embedder)
    mem0       — mem0ai SDK (cloud or self-hosted)
    zep        — Zep memory store

Backend SDKs are lazy-imported. A backend you don't configure is never
loaded; missing optional deps degrade to Noop rather than crashing.
"""

from devai.adapters.memory.base import (
    MemoryAdapter,
    MemoryRecord,
    MemoryType,
)
from devai.adapters.memory.factory import (
    KNOWN_PROVIDERS,
    create_memory_adapter,
    memory_registry,
)

__all__ = [
    "KNOWN_PROVIDERS",
    "MemoryAdapter",
    "MemoryRecord",
    "MemoryType",
    "create_memory_adapter",
    "memory_registry",
]
