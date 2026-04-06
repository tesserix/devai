"""Agent Memory System — persistent cross-run memory backed by Redis.

Provides three types of memory:
1. **Episodic** — past pipeline runs, outcomes, and learnings
2. **Semantic** — patterns, conventions, and knowledge about repos
3. **Procedural** — learned procedures, common fixes, and optimizations

Each memory entry has:
- agent: which agent created it
- repo: which repository context (or "global")
- type: episodic | semantic | procedural
- content: the memory text
- embedding_key: for similarity search
- created_at: timestamp
- access_count: how often retrieved
- relevance_score: manually set or auto-decayed

Memory is stored in Redis with TTL-based expiration for episodic memories
and permanent storage for semantic/procedural memories.

System Design for Production:
┌─────────────────────────────────────────────────────────────┐
│                    Agent Memory Architecture                │
│                                                             │
│  ┌─────────┐    ┌──────────────┐    ┌──────────────────┐   │
│  │  Agent   │───▸│ MemoryManager│───▸│  Redis Store     │   │
│  │ (any)    │◂───│              │◂───│  (persistent)    │   │
│  └─────────┘    └──────┬───────┘    └──────────────────┘   │
│                        │                                    │
│                        ▼                                    │
│                 ┌──────────────┐                            │
│                 │ Groq/Claude  │   Semantic similarity      │
│                 │ (embedding   │   search via keyword       │
│                 │  fallback)   │   matching + LLM ranking   │
│                 └──────────────┘                            │
│                                                             │
│  Memory Types:                                              │
│  ├── Episodic:  "Last run on repo X failed at review"      │
│  ├── Semantic:  "Repo X uses Go+Gin+PostgreSQL pattern"    │
│  └── Procedural:"Fix for npm ERR! 404: clear cache first"  │
│                                                             │
│  Lifecycle:                                                 │
│  ├── Episodic:  TTL 90 days, auto-decay                    │
│  ├── Semantic:  Permanent, updated on new info              │
│  └── Procedural:Permanent, scored by success rate           │
└─────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Any

from ulid import ULID

if TYPE_CHECKING:
    import redis.asyncio as redis

logger = logging.getLogger(__name__)

# TTLs
EPISODIC_TTL = 86400 * 90   # 90 days
SEMANTIC_TTL = 0             # Never expires
PROCEDURAL_TTL = 0           # Never expires

# Max memories per query
MAX_RECALL = 10


class MemoryEntry:
    """A single memory entry."""

    def __init__(
        self,
        memory_id: str = "",
        agent: str = "",
        repo: str = "global",
        memory_type: str = "episodic",
        content: str = "",
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        relevance_score: float = 1.0,
        access_count: int = 0,
        created_at: float = 0.0,
    ) -> None:
        self.memory_id = memory_id or str(ULID())
        self.agent = agent
        self.repo = repo
        self.memory_type = memory_type
        self.content = content
        self.tags = tags or []
        self.metadata = metadata or {}
        self.relevance_score = relevance_score
        self.access_count = access_count
        self.created_at = created_at or time.time()

    def to_dict(self) -> dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "agent": self.agent,
            "repo": self.repo,
            "memory_type": self.memory_type,
            "content": self.content,
            "tags": self.tags,
            "metadata": self.metadata,
            "relevance_score": self.relevance_score,
            "access_count": self.access_count,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MemoryEntry:
        return cls(**data)


class AgentMemory:
    """Persistent agent memory backed by Redis.

    Agents can store and recall memories across pipeline runs.
    Memories are scoped by agent, repo, and type for efficient retrieval.
    """

    def __init__(self, redis_client: redis.Redis) -> None:
        self.redis = redis_client

    # --- Store ---

    async def remember(
        self,
        agent: str,
        content: str,
        memory_type: str = "episodic",
        repo: str = "global",
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        relevance_score: float = 1.0,
    ) -> MemoryEntry:
        """Store a new memory.

        Args:
            agent: Agent name that created this memory.
            content: The memory content (text).
            memory_type: "episodic" | "semantic" | "procedural"
            repo: Repository context (or "global").
            tags: Searchable tags/keywords.
            metadata: Additional structured data.
            relevance_score: Initial relevance (1.0 = highest).

        Returns:
            The stored MemoryEntry.
        """
        entry = MemoryEntry(
            agent=agent,
            repo=repo,
            memory_type=memory_type,
            content=content,
            tags=tags,
            metadata=metadata,
            relevance_score=relevance_score,
        )

        key = f"devai:memory:{entry.memory_id}"
        data = json.dumps(entry.to_dict())

        # Set TTL based on type
        ttl = {
            "episodic": EPISODIC_TTL,
            "semantic": SEMANTIC_TTL,
            "procedural": PROCEDURAL_TTL,
        }.get(memory_type, EPISODIC_TTL)

        if ttl > 0:
            await self.redis.set(key, data, ex=ttl)
        else:
            await self.redis.set(key, data)

        # Index by agent
        await self.redis.sadd(f"devai:memory:idx:agent:{agent}", entry.memory_id)
        # Index by repo
        await self.redis.sadd(f"devai:memory:idx:repo:{repo}", entry.memory_id)
        # Index by type
        await self.redis.sadd(f"devai:memory:idx:type:{memory_type}", entry.memory_id)
        # Index by tags
        for tag in entry.tags:
            await self.redis.sadd(f"devai:memory:idx:tag:{tag}", entry.memory_id)
        # Timeline index
        await self.redis.zadd("devai:memory:timeline", {entry.memory_id: entry.created_at})

        logger.debug(
            "Memory stored: %s [%s/%s/%s] %s",
            entry.memory_id[:10], agent, repo, memory_type, content[:50],
        )

        return entry

    # --- Recall ---

    async def recall(
        self,
        agent: str | None = None,
        repo: str | None = None,
        memory_type: str | None = None,
        tags: list[str] | None = None,
        query: str | None = None,
        limit: int = MAX_RECALL,
    ) -> list[MemoryEntry]:
        """Recall memories matching the given filters.

        Uses set intersection for efficient filtering, then keyword
        matching for relevance ranking.

        Args:
            agent: Filter by agent name.
            repo: Filter by repo (also includes "global" memories).
            memory_type: Filter by type.
            tags: Filter by tags (any match).
            query: Free-text query for keyword matching.
            limit: Max memories to return.
        """
        # Build candidate sets
        sets_to_intersect: list[str] = []

        if agent:
            sets_to_intersect.append(f"devai:memory:idx:agent:{agent}")
        if memory_type:
            sets_to_intersect.append(f"devai:memory:idx:type:{memory_type}")

        # Get candidate IDs
        if sets_to_intersect:
            candidate_ids = await self.redis.sinter(*sets_to_intersect)
        else:
            # All memories — get from timeline
            candidate_ids = await self.redis.zrevrange("devai:memory:timeline", 0, 200)

        if not candidate_ids:
            return []

        # Repo filter: include both repo-specific AND global memories
        if repo:
            repo_ids = await self.redis.sunion(
                f"devai:memory:idx:repo:{repo}",
                "devai:memory:idx:repo:global",
            )
            candidate_ids = set(candidate_ids) & repo_ids

        # Tag filter
        if tags:
            tag_sets = [f"devai:memory:idx:tag:{t}" for t in tags]
            tag_ids = await self.redis.sunion(*tag_sets) if tag_sets else set()
            candidate_ids = set(candidate_ids) & tag_ids

        # Fetch entries
        entries: list[MemoryEntry] = []
        for mid in candidate_ids:
            mid_str = mid if isinstance(mid, str) else mid.decode()
            raw = await self.redis.get(f"devai:memory:{mid_str}")
            if raw:
                data = json.loads(raw)
                entries.append(MemoryEntry.from_dict(data))

        # Keyword search ranking
        if query:
            query_lower = query.lower()
            query_words = set(query_lower.split())

            def score(entry: MemoryEntry) -> float:
                content_lower = entry.content.lower()
                tag_match = sum(1 for t in entry.tags if t.lower() in query_lower)
                word_match = sum(1 for w in query_words if w in content_lower)
                return (
                    entry.relevance_score * 0.3
                    + tag_match * 0.3
                    + word_match * 0.2
                    + (1.0 / (1.0 + (time.time() - entry.created_at) / 86400)) * 0.2  # Recency
                )

            entries.sort(key=score, reverse=True)

        # Update access counts
        for entry in entries[:limit]:
            entry.access_count += 1
            await self.redis.set(
                f"devai:memory:{entry.memory_id}",
                json.dumps(entry.to_dict()),
                keepttl=True,
            )

        return entries[:limit]

    # --- Context Building ---

    async def build_context(
        self,
        agent: str,
        repo: str,
        query: str = "",
        limit: int = 5,
    ) -> str:
        """Build a memory context string for injection into agent prompts.

        Returns a formatted string of relevant memories for the agent.
        """
        memories = await self.recall(
            agent=agent,
            repo=repo,
            query=query,
            limit=limit,
        )

        if not memories:
            return ""

        lines = ["## Agent Memory (from past runs)\n"]
        for mem in memories:
            age_days = (time.time() - mem.created_at) / 86400
            age_str = f"{age_days:.0f}d ago" if age_days > 1 else "today"
            lines.append(
                f"- [{mem.memory_type}] ({age_str}) {mem.content}"
            )

        return "\n".join(lines)

    # --- Learning ---

    async def learn_from_run(
        self,
        agent: str,
        repo: str,
        run_id: str,
        outcome: str,
        learnings: list[str],
    ) -> list[MemoryEntry]:
        """Record learnings from a completed pipeline run.

        Called at the end of each pipeline stage to capture:
        - What worked
        - What failed and why
        - Patterns discovered
        """
        entries: list[MemoryEntry] = []

        # Episodic: record the run outcome
        entry = await self.remember(
            agent=agent,
            content=f"Run {run_id[:10]} on {repo}: {outcome}",
            memory_type="episodic",
            repo=repo,
            tags=["run_outcome", outcome],
            metadata={"run_id": run_id},
        )
        entries.append(entry)

        # Procedural: record specific learnings
        for learning in learnings:
            entry = await self.remember(
                agent=agent,
                content=learning,
                memory_type="procedural",
                repo=repo,
                tags=["learning", "fix", agent],
            )
            entries.append(entry)

        return entries

    async def learn_repo_pattern(
        self,
        repo: str,
        pattern_type: str,
        description: str,
    ) -> MemoryEntry:
        """Store a semantic memory about a repository's patterns.

        Examples:
        - "tesserix/Home-Chef-App uses Go+Gin+PostgreSQL for backend"
        - "tesserix/marketplace-admin uses Next.js 16 with @tesserix/web"
        """
        # Check if we already have this pattern — update instead of duplicate
        existing = await self.recall(
            repo=repo,
            memory_type="semantic",
            tags=[pattern_type],
            limit=1,
        )

        if existing:
            # Update existing
            entry = existing[0]
            entry.content = description
            entry.created_at = time.time()
            await self.redis.set(
                f"devai:memory:{entry.memory_id}",
                json.dumps(entry.to_dict()),
            )
            return entry

        return await self.remember(
            agent="system",
            content=description,
            memory_type="semantic",
            repo=repo,
            tags=[pattern_type, "repo_pattern"],
        )

    # --- Cleanup ---

    async def forget(self, memory_id: str) -> bool:
        """Delete a specific memory."""
        raw = await self.redis.get(f"devai:memory:{memory_id}")
        if not raw:
            return False

        data = json.loads(raw)
        entry = MemoryEntry.from_dict(data)

        # Remove from all indices
        await self.redis.srem(f"devai:memory:idx:agent:{entry.agent}", memory_id)
        await self.redis.srem(f"devai:memory:idx:repo:{entry.repo}", memory_id)
        await self.redis.srem(f"devai:memory:idx:type:{entry.memory_type}", memory_id)
        for tag in entry.tags:
            await self.redis.srem(f"devai:memory:idx:tag:{tag}", memory_id)
        await self.redis.zrem("devai:memory:timeline", memory_id)
        await self.redis.delete(f"devai:memory:{memory_id}")

        return True

    async def decay_old_memories(self, max_age_days: int = 90) -> int:
        """Decay relevance scores of old episodic memories."""
        cutoff = time.time() - (max_age_days * 86400)
        old_ids = await self.redis.zrangebyscore("devai:memory:timeline", 0, cutoff)

        decayed = 0
        for mid in old_ids:
            mid_str = mid if isinstance(mid, str) else mid.decode()
            raw = await self.redis.get(f"devai:memory:{mid_str}")
            if raw:
                data = json.loads(raw)
                if data.get("memory_type") == "episodic":
                    data["relevance_score"] = max(0.1, data.get("relevance_score", 1.0) * 0.5)
                    await self.redis.set(f"devai:memory:{mid_str}", json.dumps(data), keepttl=True)
                    decayed += 1

        return decayed
