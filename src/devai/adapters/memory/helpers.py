"""Convenience helpers shared by memory consumers.

These cover the two ergonomic surfaces the legacy `AgentMemory` had beyond
the raw adapter ABC — prompt-context formatting and dedup-on-write for repo
patterns — so call sites migrate off `AgentMemory` without losing behavior.
Adapters stay dumb storage; this glue lives above them.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from devai.adapters.memory.base import MemoryAdapter, MemoryRecord

logger = logging.getLogger(__name__)


def format_memory_context(records: list[MemoryRecord]) -> str:
    """Format records for prompt injection (legacy build_context shape)."""
    if not records:
        return ""
    lines = ["## Agent Memory (from past runs)\n"]
    now = time.time()
    for r in records:
        age_days = (now - r.created_at) / 86400
        age_str = f"{age_days:.0f}d ago" if age_days > 1 else "today"
        mt = getattr(r.memory_type, "value", r.memory_type)
        lines.append(f"- [{mt}] ({age_str}) {r.content}")
    return "\n".join(lines)


async def remember_repo_pattern(
    memory: MemoryAdapter,
    *,
    repo: str,
    pattern_type: str,
    description: str,
    agent: str = "",
) -> bool:
    """Store a semantic repo pattern, skipping exact duplicates.

    tech_detector / db_engineer fire on every pipeline run; without this
    check the corpus fills with identical "Tech stack: {...}" rows. Real
    near-duplicate merging is the Phase D maintenance job's business — this
    only suppresses byte-identical re-writes.
    """
    try:
        existing = await memory.recall(
            repo=repo,
            memory_type="semantic",
            tags=[pattern_type],
            limit=5,
        )
        if any(r.content == description for r in existing):
            return False
        await memory.remember(
            description,
            agent=agent,
            repo=repo,
            memory_type="semantic",
            tags=["repo_pattern", pattern_type],
            metadata={"pattern_type": pattern_type},
        )
        return True
    except Exception:  # noqa: BLE001 — learning is best-effort, never break the caller
        logger.exception("remember_repo_pattern failed (pattern_type=%s repo=%s)", pattern_type, repo)
        return False


__all__ = ["format_memory_context", "remember_repo_pattern"]
