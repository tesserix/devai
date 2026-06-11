"""Process-global memory adapter accessor.

Mirrors `adapters.telemetry.runtime`: the app builds ONE memory adapter at
startup (webhook app / pipeline bootstrap) and registers it here. Call sites
created outside the StageDeps wiring — the chat agent's tools, legacy agents
(db_engineer, tech_detector), the old LangGraph orchestrator, dashboard
routes — read the adapter via `get_global_memory()` and always get a usable
instance: the Noop until an app registers a real one.

This is NOT a substitute for StageDeps injection where that exists
(pipeline stages take `deps.memory` directly); it's the consolidation path
that lets every legacy `AgentMemory(redis)` call site speak MemoryAdapter,
so the configured provider (pgvector in prod) is honored everywhere.
"""

from __future__ import annotations

from devai.adapters.memory.base import MemoryAdapter
from devai.adapters.memory.noop import NoopMemoryAdapter

_global: MemoryAdapter = NoopMemoryAdapter()


def set_global_memory(adapter: MemoryAdapter | None) -> None:
    """Register the process-wide memory adapter. None resets to Noop."""
    global _global
    _global = adapter if adapter is not None else NoopMemoryAdapter()


def get_global_memory() -> MemoryAdapter:
    """The process-wide adapter. Never None — Noop until an app registers one."""
    return _global


__all__ = ["get_global_memory", "set_global_memory"]
