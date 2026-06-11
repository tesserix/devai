"""Telemetry-instrumented memory adapter delegate.

Wraps any concrete `MemoryAdapter` and records one counter + one duration
observation per operation into the process-global telemetry sink
(`adapters.telemetry.runtime`), mirroring `InstrumentedLLMAdapter`. The
factory applies this wrapper to every backend it builds, so EVERY caller —
memory_injection, the learn stages, chat tools, the scan route — emits
memory telemetry with zero changes at the call site.

Instruments:
    devai.memory.ops          counter   {provider, op, status}
    devai.memory.duration_ms  histogram {provider, op}
    devai.memory.results      histogram {provider, op}   (recall/search hits)

When the sink is the Noop the overhead is two attribute reads — free.
"""

from __future__ import annotations

import time
from typing import Any

from devai.adapters.memory.base import MemoryAdapter, MemoryRecord, MemoryType


class InstrumentedMemoryAdapter(MemoryAdapter):
    """Pure delegate: forwards everything, records usage on the way out."""

    def __init__(self, inner: MemoryAdapter) -> None:
        self._inner = inner
        self.provider_name = inner.provider_name

    # ── Instrumented surface ─────────────────────────────────────────

    async def remember(
        self,
        content: str,
        *,
        agent: str = "",
        repo: str = "global",
        memory_type: MemoryType | str = MemoryType.EPISODIC,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        relevance_score: float = 1.0,
    ) -> MemoryRecord:
        started = time.perf_counter()
        try:
            record = await self._inner.remember(
                content,
                agent=agent,
                repo=repo,
                memory_type=memory_type,
                tags=tags,
                metadata=metadata,
                relevance_score=relevance_score,
            )
        except Exception:
            self._record("remember", started, status="error")
            raise
        self._record("remember", started)
        return record

    async def recall(
        self,
        *,
        query: str | None = None,
        agent: str | None = None,
        repo: str | None = None,
        memory_type: MemoryType | str | None = None,
        tags: list[str] | None = None,
        limit: int = 10,
    ) -> list[MemoryRecord]:
        started = time.perf_counter()
        try:
            records = await self._inner.recall(
                query=query,
                agent=agent,
                repo=repo,
                memory_type=memory_type,
                tags=tags,
                limit=limit,
            )
        except Exception:
            self._record("recall", started, status="error")
            raise
        self._record("recall", started, results=len(records))
        return records

    async def semantic_search(
        self,
        query: str,
        *,
        k: int = 5,
        agent: str | None = None,
        repo: str | None = None,
        memory_type: MemoryType | str | None = None,
    ) -> list[MemoryRecord]:
        started = time.perf_counter()
        try:
            records = await self._inner.semantic_search(
                query,
                k=k,
                agent=agent,
                repo=repo,
                memory_type=memory_type,
            )
        except Exception:
            self._record("semantic_search", started, status="error")
            raise
        self._record("semantic_search", started, results=len(records))
        return records

    async def forget(self, provider_id: str) -> bool:
        started = time.perf_counter()
        try:
            removed = await self._inner.forget(provider_id)
        except Exception:
            self._record("forget", started, status="error")
            raise
        self._record("forget", started)
        return removed

    async def reinforce(self, provider_ids: list[str]) -> int:
        started = time.perf_counter()
        try:
            count = await self._inner.reinforce(provider_ids)
        except Exception:
            self._record("reinforce", started, status="error")
            raise
        self._record("reinforce", started, results=count)
        return count

    # ── Pass-throughs ────────────────────────────────────────────────

    async def close(self) -> None:
        await self._inner.close()

    async def health_check(self) -> dict[str, Any]:
        return await self._inner.health_check()

    # ── Internal ─────────────────────────────────────────────────────

    def _record(self, op: str, started: float, *, status: str = "ok", results: int | None = None) -> None:
        try:
            from devai.adapters.telemetry.runtime import get_global_telemetry

            sink = get_global_telemetry()
            attrs = {"provider": self.provider_name, "op": op}
            sink.incr("devai.memory.ops", attrs={**attrs, "status": status})
            sink.observe("devai.memory.duration_ms", (time.perf_counter() - started) * 1000.0, attrs=attrs)
            if results is not None:
                sink.observe("devai.memory.results", float(results), attrs=attrs)
        except Exception:  # noqa: BLE001 — telemetry must never break the call
            pass


__all__ = ["InstrumentedMemoryAdapter"]
