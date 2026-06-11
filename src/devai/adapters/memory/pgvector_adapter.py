"""pgvector memory adapter — DevAI's natural production default.

DevAI already runs Postgres with the `pgvector` extension and an
`agent_memories` table (see `db/migrations/`). This adapter wraps the
existing `devai.services.database.Database` so the new pipeline picks
up everything that's been persisted across runs since day one.

Embeddings: we use whatever the configured embedding provider returns
(OpenAI text-embedding-3-small / Groq / Anthropic). When no embedding
provider is configured we fall back to keyword recall — the adapter
still works, just without similarity ranking.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import TYPE_CHECKING, Any

from devai.adapters.base import AdapterNotConfigured
from devai.adapters.memory.base import MemoryAdapter, MemoryRecord, MemoryType

if TYPE_CHECKING:
    from devai.services.database import Database

logger = logging.getLogger(__name__)


class PgVectorMemoryAdapter(MemoryAdapter):
    """Adapter over the existing `agent_memories` table + pgvector index.

    Reuses `Database.semantic_search()` for embedding lookup and writes
    via raw INSERT so we don't need to add another method on Database.
    """

    provider_name = "pgvector"

    def __init__(self, database: Database, *, embedder: Any = None) -> None:
        self._db = database
        self._embedder = embedder  # any object exposing `embed(text) -> list[float]`

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
        if self._db is None or self._db.pool is None:
            raise AdapterNotConfigured("pgvector adapter requires a connected Database")

        mt = MemoryType.parse(memory_type)
        memory_id = uuid.uuid4()
        now = time.time()

        embedding = await self._maybe_embed(content)

        # Schema borrowed from db/migrations — `agent_memories(id, agent, repo,
        # memory_type, content, tags, metadata, relevance_score, embedding,
        # created_at, is_active)`. Adjust if the migration evolves.
        await self._db.pool.execute(
            """INSERT INTO agent_memories
                   (id, agent, repo, memory_type, content, tags, metadata,
                    relevance_score, embedding, created_at, is_active)
               VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9::vector, NOW(), TRUE)
               ON CONFLICT (id) DO NOTHING""",
            memory_id,
            agent,
            repo,
            mt.value,
            content,
            list(tags or []),
            _to_jsonb(metadata or {}),
            relevance_score,
            embedding,
        )

        return MemoryRecord(
            content=content,
            agent=agent,
            repo=repo,
            memory_type=mt,
            tags=list(tags or []),
            metadata=dict(metadata or {}),
            relevance_score=relevance_score,
            created_at=now,
            provider_id=str(memory_id),
            provider=self.provider_name,
        )

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
        if self._db is None or self._db.pool is None:
            return []

        clauses = ["is_active = TRUE"]
        params: list[Any] = []
        if agent:
            params.append(agent)
            clauses.append(f"agent = ${len(params)}")
        if repo:
            params.append(repo)
            clauses.append(f"(repo = ${len(params)} OR repo = 'global')")
        if memory_type:
            params.append(MemoryType.parse(memory_type).value)
            clauses.append(f"memory_type = ${len(params)}")
        if tags:
            params.append(list(tags))
            clauses.append(f"tags && ${len(params)}::text[]")
        if query:
            params.append(f"%{query}%")
            clauses.append(f"content ILIKE ${len(params)}")
        params.append(limit)
        sql = (
            f"SELECT * FROM agent_memories WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT ${len(params)}"
        )
        rows = await self._db.pool.fetch(sql, *params)
        return [self._row_to_record(dict(r)) for r in rows]

    async def semantic_search(
        self,
        query: str,
        *,
        k: int = 5,
        agent: str | None = None,
        repo: str | None = None,
        memory_type: MemoryType | str | None = None,
    ) -> list[MemoryRecord]:
        if self._db is None or self._db.pool is None:
            return []

        embedding = await self._maybe_embed(query)
        if embedding is None:
            # No embedder available — degrade to keyword recall
            return await self.recall(query=query, agent=agent, repo=repo, memory_type=memory_type, limit=k)

        rows = await self._db.semantic_search(
            embedding=embedding,
            repo=repo,
            limit=k,
            agent=agent,
            memory_type=MemoryType.parse(memory_type).value if memory_type else None,
        )
        return [self._row_to_record(row) for row in rows]

    # ── Delete ────────────────────────────────────────────────────────

    async def forget(self, provider_id: str) -> bool:
        if self._db is None or self._db.pool is None:
            return False
        result = await self._db.pool.execute(
            "UPDATE agent_memories SET is_active = FALSE WHERE id = $1::uuid",
            provider_id,
        )
        # `UPDATE 0` vs `UPDATE 1`
        return result.split()[-1] != "0"

    # ── Adapter contract ──────────────────────────────────────────────

    async def close(self) -> None:
        # Database pool is owned by the host — don't close it here. The
        # embedder is ours though (the factory builds it for this adapter),
        # and it may hold an HTTP client that must be released.
        closer = getattr(self._embedder, "close", None)
        if closer is not None:
            try:
                await closer()
            except Exception:  # noqa: BLE001
                logger.debug("embedder close failed", exc_info=True)

    async def health_check(self) -> dict[str, Any]:
        try:
            if self._db is None or self._db.pool is None:
                return {"ok": False, "provider": self.provider_name, "detail": "db not connected"}
            # Connectivity probe only — this runs on every /readyz poll, so a
            # COUNT(*) over a growing table would tax the DB for no signal.
            await self._db.pool.fetchval("SELECT 1")
            return {
                "ok": True,
                "provider": self.provider_name,
                "detail": f"connected (embeddings {'on' if self._embedder is not None else 'off'})",
            }
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "provider": self.provider_name, "detail": str(e)}

    # ── Internal ──────────────────────────────────────────────────────

    async def _maybe_embed(self, text: str) -> list[float] | None:
        if self._embedder is None:
            return None
        try:
            embed = getattr(self._embedder, "embed", None)
            if embed is None:
                return None
            result = embed(text)
            # Accept sync and async embedders
            import inspect as _inspect

            if _inspect.isawaitable(result):
                result = await result
            return list(result)
        except Exception:  # noqa: BLE001
            logger.exception("pgvector embedder failed — falling back to no embedding")
            return None

    def _row_to_record(self, row: dict[str, Any]) -> MemoryRecord:
        meta = row.get("metadata") or {}
        if isinstance(meta, str):
            import json as _json

            try:
                meta = _json.loads(meta)
            except Exception:  # noqa: BLE001
                meta = {}

        return MemoryRecord(
            content=row.get("content", ""),
            agent=row.get("agent", ""),
            repo=row.get("repo", "global"),
            memory_type=MemoryType.parse(row.get("memory_type")),
            tags=list(row.get("tags") or []),
            metadata=dict(meta),
            relevance_score=float(row.get("relevance_score", 1.0)),
            similarity=row.get("similarity"),
            access_count=int(row.get("access_count", 0) or 0),
            created_at=_to_epoch(row.get("created_at")),
            provider_id=str(row.get("id")),
            provider=self.provider_name,
        )


def _to_jsonb(value: Any) -> str:
    import json as _json

    return _json.dumps(value)


def _to_epoch(value: Any) -> float:
    """Convert a Postgres TIMESTAMP / datetime / float to epoch seconds."""
    if value is None:
        return time.time()
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(value.timestamp())  # type: ignore[attr-defined]
    except AttributeError:
        return time.time()


__all__ = ["PgVectorMemoryAdapter"]
