"""Noop memory backend.

Used when:
  - `DEVAI_MEMORY_PROVIDER=noop` (explicit opt-out)
  - The configured provider's SDK isn't installed (graceful degrade)
  - The configured provider's required settings are missing
  - Unit tests run without a real backing store

Stores nothing, returns empty everywhere. `remember()` returns a valid
MemoryRecord so callers that capture the result still get a sensible
shape; nothing is persisted.

Optionally keeps an in-memory list when `keep_in_memory=True` for tests
that want to exercise the round-trip path without standing up Redis or
Postgres.
"""

from __future__ import annotations

from typing import Any

from devai.adapters.memory.base import MemoryAdapter, MemoryRecord, MemoryType


class NoopMemoryAdapter(MemoryAdapter):
    """Records nothing — or records to a process-local list when asked.

    The `keep_in_memory` flag turns it into a useful test fake: the same
    surface as the real adapter, but lives in a list. Default is False
    (truly noop) so production never accidentally grows unbounded memory.
    """

    provider_name = "noop"

    def __init__(self, *, keep_in_memory: bool = False) -> None:
        self._keep = keep_in_memory
        self._store: list[MemoryRecord] = []

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
        record = MemoryRecord(
            content=content,
            agent=agent,
            repo=repo,
            memory_type=MemoryType.parse(memory_type),
            tags=list(tags or []),
            metadata=dict(metadata or {}),
            relevance_score=relevance_score,
            provider=self.provider_name,
        )
        if self._keep:
            self._store.append(record)
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
        if not self._keep:
            return []
        wanted_type = MemoryType.parse(memory_type) if memory_type else None
        wanted_tags = set(tags or [])
        # For queries, do a token-OR match (any word in the query appears
        # in the content). This mirrors the existing AgentMemory keyword
        # scoring and makes Noop a useful test fake without overstating
        # what it can do — there's still no embedding-based similarity.
        query_tokens = (
            {tok for tok in query.lower().split() if len(tok) > 2}
            if query
            else set()
        )

        out: list[MemoryRecord] = []
        for record in reversed(self._store):  # newest first
            if agent and record.agent != agent:
                continue
            if repo and record.repo != repo:
                continue
            if wanted_type and record.memory_type != wanted_type:
                continue
            if wanted_tags and not wanted_tags.issubset(set(record.tags)):
                continue
            if query_tokens:
                content_lower = record.content.lower()
                if not any(tok in content_lower for tok in query_tokens):
                    continue
            out.append(record)
            if len(out) >= limit:
                break
        return out

    async def semantic_search(
        self,
        query: str,
        *,
        k: int = 5,
        agent: str | None = None,
        repo: str | None = None,
        memory_type: MemoryType | str | None = None,
    ) -> list[MemoryRecord]:
        # No embeddings — degrade to substring recall
        return await self.recall(
            query=query, agent=agent, repo=repo, memory_type=memory_type, limit=k
        )

    async def forget(self, provider_id: str) -> bool:
        if not self._keep:
            return False
        before = len(self._store)
        self._store = [r for r in self._store if r.provider_id != provider_id]
        return len(self._store) != before

    async def health_check(self) -> dict[str, Any]:
        return {
            "ok": True,
            "provider": self.provider_name,
            "detail": f"in_memory={self._keep}, count={len(self._store)}",
        }


__all__ = ["NoopMemoryAdapter"]
