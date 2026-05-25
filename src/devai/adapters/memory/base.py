"""MemoryAdapter ABC + canonical record types.

The ABC declares the smallest surface every memory backend must implement.
Everything that calls `agent.memory.X` in DevAI should go through this
interface so swapping mem0 ↔ zep ↔ pgvector is one env-var change.

`MemoryRecord` is the lingua franca: every adapter returns these,
regardless of backing store. The fields mirror the existing
`devai.services.memory.MemoryEntry` so the migration is a swap, not a
rewrite.
"""

from __future__ import annotations

import time
import uuid
from abc import abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from devai.adapters.base import Adapter


class MemoryType(str, Enum):
    """The three-way split DevAI's agents already use.

    - **episodic**  — what happened in a past run (TTL 90d)
    - **semantic**  — learned facts about a repo/codebase (no TTL)
    - **procedural** — how-to knowledge / recipes (no TTL)
    """

    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"

    @classmethod
    def parse(cls, value: str | MemoryType | None, *, default: MemoryType | None = None) -> MemoryType:
        if value is None:
            return default or cls.EPISODIC
        if isinstance(value, cls):
            return value
        try:
            return cls(value)
        except ValueError:
            return default or cls.EPISODIC


@dataclass(slots=True)
class MemoryRecord:
    """A single memory entry — adapter-agnostic.

    Every adapter normalizes its native row shape into this dataclass on
    the way out. The `provider_id` is the backend-native id (mem0 returns
    UUIDs, zep returns ULIDs, pgvector returns UUIDs) — we keep it
    transparent so `forget(record.provider_id)` always works.
    """

    content: str
    agent: str = ""
    repo: str = "global"
    memory_type: MemoryType = MemoryType.EPISODIC
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    relevance_score: float = 1.0
    similarity: float | None = None  # populated by semantic_search
    access_count: int = 0
    created_at: float = field(default_factory=time.time)
    provider_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    provider: str = ""  # filled in by the adapter that produced it

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "provider": self.provider,
            "agent": self.agent,
            "repo": self.repo,
            "memory_type": self.memory_type.value,
            "content": self.content,
            "tags": list(self.tags),
            "metadata": dict(self.metadata),
            "relevance_score": self.relevance_score,
            "similarity": self.similarity,
            "access_count": self.access_count,
            "created_at": self.created_at,
        }


class MemoryAdapter(Adapter):
    """The minimum surface every memory backend implements.

    Five operations:
      - `remember`         — write a record
      - `recall`           — keyword/filter lookup (no embedding required)
      - `semantic_search`  — embedding-similarity lookup
      - `forget`           — delete by provider_id
      - `close` + `health_check` — inherited Adapter contract

    Backends without native semantic search (Noop, plain Redis) implement
    `semantic_search` by delegating to `recall` and accept the
    degradation. The CALLER never branches on backend — pick the best
    fit for the deployment and the rest of the code is identical.
    """

    #: One of the values in MEMORY_PROVIDER_NAMES — set by subclass.
    provider_name: str = "abstract"

    # ──────────────────────────────────────────────────────────────────
    # Write
    # ──────────────────────────────────────────────────────────────────

    @abstractmethod
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
        """Persist a memory and return the stored MemoryRecord."""

    # ──────────────────────────────────────────────────────────────────
    # Read
    # ──────────────────────────────────────────────────────────────────

    @abstractmethod
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
        """Return memories matching all provided filters.

        `query` is a free-text search when the backend supports it; pure
        keyword adapters use substring matching. Combining `query` with
        other filters is AND.
        """

    @abstractmethod
    async def semantic_search(
        self,
        query: str,
        *,
        k: int = 5,
        agent: str | None = None,
        repo: str | None = None,
        memory_type: MemoryType | str | None = None,
    ) -> list[MemoryRecord]:
        """Return the top-k semantically similar memories.

        Backends without embeddings (Noop, plain Redis) delegate to
        `recall(query=...)`. The `similarity` field on each returned
        record is populated when the backend supports it; None otherwise.
        """

    # ──────────────────────────────────────────────────────────────────
    # Delete
    # ──────────────────────────────────────────────────────────────────

    @abstractmethod
    async def forget(self, provider_id: str) -> bool:
        """Delete by id. Returns True if a record was removed."""

    # ──────────────────────────────────────────────────────────────────
    # Adapter contract — implemented as defaults; subclasses override.
    # ──────────────────────────────────────────────────────────────────

    async def close(self) -> None:
        """No-op by default — subclasses that hold connections override."""
        return None

    async def health_check(self) -> dict[str, Any]:
        return {"ok": True, "provider": self.provider_name, "detail": "no health check implemented"}


__all__ = ["MemoryAdapter", "MemoryRecord", "MemoryType"]
