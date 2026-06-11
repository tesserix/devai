"""Memory lifecycle maintenance — TTL purge, relevance decay, near-dup merge.

Runs against the Postgres `agent_memories` store (the pgvector backend).
Vendor backends (mem0/zep/hondo) manage their own lifecycle; redis episodic
records already carry native TTLs — so when the configured provider isn't
pgvector, or Postgres is unreachable, every pass is a logged no-op.

Scheduled by the webhook app's lifespan as a periodic task
(`DEVAI_MEMORY_MAINTENANCE_HOURS`, 0 disables). Every operation is
idempotent and bounded, so overlapping or repeated passes are safe:

  purge  — soft-delete episodic records past their TTL and anything whose
           explicit `expires_at` has passed.
  decay  — nudge relevance down (×0.98, floor 0.1) for records not recalled
           in 30 days, so stale-but-undeleted knowledge stops outranking
           fresh learnings.
  dedup  — deactivate older near-duplicates (cosine distance < 1-threshold,
           same repo + memory_type) and reinforce the surviving newest copy.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def run_memory_maintenance(
    db: Any,
    *,
    episodic_ttl_days: int = 90,
    decay_idle_days: int = 30,
    dedup_similarity: float = 0.95,
) -> dict[str, int]:
    """One maintenance pass. Returns per-operation affected-row counts."""
    stats = {"purged": 0, "expired": 0, "decayed": 0, "deduped": 0}
    if db is None:
        return stats
    try:
        pool = db.pool
    except Exception:  # noqa: BLE001 — Database.pool raises when unconnected
        return stats
    if pool is None:
        return stats

    # ── purge: episodic TTL + explicit expires_at ─────────────────────
    try:
        result = await pool.execute(
            """UPDATE agent_memories SET is_active = FALSE
               WHERE is_active = TRUE AND memory_type = 'episodic'
                 AND created_at < NOW() - make_interval(days => $1)""",
            episodic_ttl_days,
        )
        stats["purged"] = _affected(result)
        result = await pool.execute(
            """UPDATE agent_memories SET is_active = FALSE
               WHERE is_active = TRUE AND expires_at IS NOT NULL AND expires_at < NOW()"""
        )
        stats["expired"] = _affected(result)
    except Exception:  # noqa: BLE001
        logger.exception("memory maintenance: purge pass failed")

    # ── decay: idle records lose a little relevance each pass ─────────
    try:
        result = await pool.execute(
            """UPDATE agent_memories
               SET relevance_score = GREATEST(relevance_score * 0.98, 0.1)
               WHERE is_active = TRUE
                 AND relevance_score > 0.1
                 AND COALESCE(last_accessed, created_at) < NOW() - make_interval(days => $1)""",
            decay_idle_days,
        )
        stats["decayed"] = _affected(result)
    except Exception:  # noqa: BLE001
        logger.exception("memory maintenance: decay pass failed")

    # ── dedup: deactivate older near-duplicates, keep + boost newest ──
    # Pairwise within (repo, memory_type) so the scan stays bounded; the
    # HNSW index doesn't help a self-join, but the corpus this targets is
    # the slow accumulation of per-run writes — thousands, not millions.
    try:
        rows = await pool.fetch(
            """SELECT a.id AS old_id, b.id AS keep_id
               FROM agent_memories a
               JOIN agent_memories b
                 ON b.repo = a.repo AND b.memory_type = a.memory_type
                AND b.id <> a.id AND b.created_at > a.created_at
                AND a.embedding IS NOT NULL AND b.embedding IS NOT NULL
                AND (a.embedding <=> b.embedding) < $1
               WHERE a.is_active = TRUE AND b.is_active = TRUE
               LIMIT 200""",
            1.0 - dedup_similarity,
        )
        old_ids = list({r["old_id"] for r in rows})
        keep_ids = list({r["keep_id"] for r in rows})
        if old_ids:
            await pool.execute(
                "UPDATE agent_memories SET is_active = FALSE WHERE id = ANY($1)",
                old_ids,
            )
            await pool.execute(
                """UPDATE agent_memories
                   SET relevance_score = LEAST(relevance_score + 0.1, 2.0)
                   WHERE id = ANY($1)""",
                keep_ids,
            )
            stats["deduped"] = len(old_ids)
    except Exception:  # noqa: BLE001
        logger.exception("memory maintenance: dedup pass failed")

    try:
        from devai.adapters.telemetry.runtime import get_global_telemetry

        sink = get_global_telemetry()
        for op, count in stats.items():
            sink.incr("devai.memory.maintenance", float(count), attrs={"op": op})
    except Exception:  # noqa: BLE001
        pass

    logger.info("memory maintenance pass: %s", stats)
    return stats


def _affected(result: Any) -> int:
    """asyncpg execute() returns e.g. 'UPDATE 3' — extract the count."""
    try:
        return int(str(result).split()[-1])
    except (ValueError, IndexError):
        return 0


__all__ = ["run_memory_maintenance"]
