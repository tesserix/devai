"""Redis-backed state manager for pipeline run tracking."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import redis.asyncio as redis

from devai.models import AgentResult, PipelineContext

logger = logging.getLogger(__name__)


class StateManager:
    """Manages pipeline state in Redis."""

    def __init__(self, redis_url: str, result_ttl: int = 86400 * 30, lock_ttl: int = 360) -> None:
        self.redis: redis.Redis = redis.from_url(redis_url, decode_responses=True)  # type: ignore[assignment]
        self.result_ttl = result_ttl
        self.lock_ttl = lock_ttl

    # --- Pipeline Run Lifecycle ---

    async def create_run(self, ctx: PipelineContext) -> None:
        """Initialize a new pipeline run in Redis."""
        now = time.time()
        pipe = self.redis.pipeline()
        pipe.hset(
            f"devai:run:{ctx.run_id}",
            mapping={
                "context": ctx.model_dump_json(),
                "stage": ctx.stage.value,
                "repo": ctx.repo_full_name,
                "created_at": str(now),
                "updated_at": str(now),
            },
        )
        pipe.zadd("devai:runs:by_time", {ctx.run_id: now})
        pipe.lpush("devai:runs:recent", ctx.run_id)
        pipe.ltrim("devai:runs:recent", 0, 499)
        pipe.lpush(f"devai:runs:by_repo:{ctx.repo_full_name}", ctx.run_id)
        pipe.ltrim(f"devai:runs:by_repo:{ctx.repo_full_name}", 0, 99)
        await pipe.execute()
        logger.info("Created pipeline run %s for %s", ctx.run_id, ctx.repo_full_name)

    async def update_run_stage(self, run_id: str, stage: str) -> None:
        """Update the stage of a pipeline run."""
        await self.redis.hset(
            f"devai:run:{run_id}",
            mapping={"stage": stage, "updated_at": str(time.time())},
        )

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        """Get a pipeline run's state."""
        data = await self.redis.hgetall(f"devai:run:{run_id}")
        if not data:
            return None
        if "context" in data:
            data["context"] = json.loads(data["context"])
        return data

    async def list_runs(self, limit: int = 20) -> list[str]:
        """List recent pipeline run IDs."""
        return await self.redis.lrange("devai:runs:recent", 0, limit - 1)

    async def list_runs_by_repo(self, repo: str, limit: int = 20) -> list[str]:
        """List recent pipeline run IDs for a specific repo."""
        return await self.redis.lrange(f"devai:runs:by_repo:{repo}", 0, limit - 1)

    # --- Agent Status ---

    async def set_agent_status(
        self,
        run_id: str,
        agent_name: str,
        status: str,
        error: str | None = None,
    ) -> None:
        """Update an agent's status within a pipeline run."""
        payload = json.dumps(
            {
                "status": status,
                "updated_at": time.time(),
                "error": error,
            }
        )
        await self.redis.hset(f"devai:run:{run_id}:agents", agent_name, payload)

    async def get_agent_statuses(self, run_id: str) -> dict[str, Any]:
        """Get all agent statuses for a pipeline run."""
        raw = await self.redis.hgetall(f"devai:run:{run_id}:agents")
        return {k: json.loads(v) for k, v in raw.items()}

    # --- Agent Results ---

    async def store_agent_result(self, run_id: str, agent_name: str, result: AgentResult) -> None:
        """Store an agent's result with TTL."""
        await self.redis.set(
            f"devai:run:{run_id}:result:{agent_name}",
            result.model_dump_json(),
            ex=self.result_ttl,
        )

    async def get_agent_result(self, run_id: str, agent_name: str) -> AgentResult | None:
        """Retrieve an agent's result."""
        raw = await self.redis.get(f"devai:run:{run_id}:result:{agent_name}")
        if raw is None:
            return None
        return AgentResult.model_validate_json(raw)

    # --- Distributed Locking ---

    async def acquire_lock(self, run_id: str, agent_name: str, worker_id: str) -> bool:
        """Acquire a distributed lock for an agent on a run. Returns True if acquired."""
        return bool(
            await self.redis.set(
                f"devai:lock:{run_id}:{agent_name}",
                worker_id,
                nx=True,
                ex=self.lock_ttl,
            )
        )

    async def release_lock(self, run_id: str, agent_name: str, worker_id: str) -> bool:
        """Release a lock only if we own it."""
        key = f"devai:lock:{run_id}:{agent_name}"
        current = await self.redis.get(key)
        if current == worker_id:
            await self.redis.delete(key)
            return True
        return False

    # --- Cleanup ---

    async def close(self) -> None:
        """Close the Redis connection."""
        await self.redis.aclose()
