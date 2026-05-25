"""Redis-backed memory adapter.

Wraps the existing `devai.services.memory.AgentMemory` so flipping
`DEVAI_MEMORY_PROVIDER=redis` is a zero-effort backward-compat mode.
Every method delegates to AgentMemory and normalizes the returned
`MemoryEntry` objects into our adapter-side `MemoryRecord`s.

No native embedding support. `semantic_search` degrades to `recall`
with `query=` substring matching, which is what the existing
implementation already does.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from devai.adapters.memory.base import MemoryAdapter, MemoryRecord, MemoryType

if TYPE_CHECKING:
    import redis.asyncio as redis

    from devai.services.memory import MemoryEntry

logger = logging.getLogger(__name__)


class RedisMemoryAdapter(MemoryAdapter):
    """Adapter over `devai.services.memory.AgentMemory`."""

    provider_name = "redis"

    def __init__(self, redis_client: "redis.Redis") -> None:
        # Lazy import so the noop-config path never has to load the
        # ulid / redis dependency chain.
        from devai.services.memory import AgentMemory

        self._memory = AgentMemory(redis_client)
        self._redis = redis_client

    # ── Write ─────────────────────────────────────────────────────────

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
        mt = MemoryType.parse(memory_type)
        entry = await self._memory.remember(
            agent=agent,
            content=content,
            memory_type=mt.value,
            repo=repo,
            tags=list(tags or []),
            metadata=dict(metadata or {}),
            relevance_score=relevance_score,
        )
        return self._entry_to_record(entry)

    # ── Read ──────────────────────────────────────────────────────────

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
        mt_value = MemoryType.parse(memory_type).value if memory_type else None
        entries = await self._memory.recall(
            agent=agent,
            repo=repo,
            memory_type=mt_value,
            tags=list(tags) if tags else None,
            query=query,
            limit=limit,
        )
        return [self._entry_to_record(e) for e in entries]

    async def semantic_search(
        self,
        query: str,
        *,
        k: int = 5,
        agent: str | None = None,
        repo: str | None = None,
        memory_type: MemoryType | str | None = None,
    ) -> list[MemoryRecord]:
        # AgentMemory does keyword-based scoring inside `recall(query=...)` —
        # closest thing this backend has to semantic search.
        return await self.recall(
            query=query, agent=agent, repo=repo, memory_type=memory_type, limit=k
        )

    # ── Delete ────────────────────────────────────────────────────────

    async def forget(self, provider_id: str) -> bool:
        return await self._memory.forget(provider_id)

    # ── Adapter contract ──────────────────────────────────────────────

    async def close(self) -> None:
        # We don't own the redis client (PipelineService / app does) — don't
        # close it here, or unrelated callers lose their connection.
        return None

    async def health_check(self) -> dict[str, Any]:
        try:
            await self._redis.ping()
            return {"ok": True, "provider": self.provider_name, "detail": "redis ping ok"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "provider": self.provider_name, "detail": f"redis ping failed: {e}"}

    # ── Internal ──────────────────────────────────────────────────────

    def _entry_to_record(self, entry: "MemoryEntry") -> MemoryRecord:
        return MemoryRecord(
            content=entry.content,
            agent=entry.agent,
            repo=entry.repo,
            memory_type=MemoryType.parse(entry.memory_type),
            tags=list(entry.tags),
            metadata=dict(entry.metadata),
            relevance_score=entry.relevance_score,
            access_count=entry.access_count,
            created_at=entry.created_at,
            provider_id=entry.memory_id,
            provider=self.provider_name,
        )


__all__ = ["RedisMemoryAdapter"]
