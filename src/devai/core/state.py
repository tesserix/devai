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

    # --- Pipeline tasks (Fiber-style runtime) ---
    #
    # Namespaced under `devai:pipeline:*` so they don't collide with the
    # legacy `devai:run:*` keys used by the LangGraph orchestrator. Both
    # surfaces can coexist while we cut over.

    PIPELINE_TASK_KEY = "devai:pipeline:task:{task_id}"
    PIPELINE_RECENT_KEY = "devai:pipeline:tasks:recent"
    PIPELINE_BY_BLUEPRINT_KEY = "devai:pipeline:tasks:by_blueprint:{blueprint}"
    PIPELINE_BY_REPO_KEY = "devai:pipeline:tasks:by_repo:{repo}"
    PIPELINE_CONTROL_KEY = "devai:pipeline:control:{task_id}"

    async def persist_task(self, task_dict: dict[str, Any], *, ttl: int | None = None) -> None:
        """Write a DevAITask dict to Redis.

        Idempotent — the Pipeline calls this after every state mutation,
        so the latest snapshot always wins. The recent / by_blueprint /
        by_repo indices are upserted on first persist; later persists
        just overwrite the snapshot.
        """
        task_id = task_dict.get("id")
        if not task_id:
            logger.warning("persist_task: dict missing 'id' — ignored")
            return

        blueprint = task_dict.get("blueprint", "unknown")
        repo = task_dict.get("repo", "")
        ttl = ttl if ttl is not None else self.result_ttl
        now = time.time()

        payload = json.dumps(task_dict)
        pipe = self.redis.pipeline()
        pipe.set(self.PIPELINE_TASK_KEY.format(task_id=task_id), payload, ex=ttl)
        # Sorted set keyed by updated_at so we can list "most recent"
        pipe.zadd(self.PIPELINE_RECENT_KEY, {task_id: task_dict.get("updated_at", now)})
        # Cap the recent index at 1000 entries
        pipe.zremrangebyrank(self.PIPELINE_RECENT_KEY, 0, -1001)
        if blueprint:
            pipe.zadd(self.PIPELINE_BY_BLUEPRINT_KEY.format(blueprint=blueprint), {task_id: now})
        if repo:
            pipe.zadd(self.PIPELINE_BY_REPO_KEY.format(repo=repo), {task_id: now})
        await pipe.execute()

    async def get_pipeline_task(self, task_id: str) -> dict[str, Any] | None:
        """Read back a previously-persisted task dict."""
        raw = await self.redis.get(self.PIPELINE_TASK_KEY.format(task_id=task_id))
        if raw is None:
            return None
        return json.loads(raw)

    # --- Pipeline run control (pause / resume / stop) ---
    #
    # A single flag per task that the in-process BlueprintExecutor polls
    # between stages: "paused" blocks at the next stage boundary, "stopped"
    # cancels the run, anything else (or absent) runs normally. Under the
    # Temporal backend these become workflow Signals (later phase); this
    # Redis flag drives the default in-process path.

    async def set_pipeline_control(self, task_id: str, value: str, *, ttl: int = 86400) -> None:
        """Set the run-control flag. `running`/`resume`/empty clears it."""
        key = self.PIPELINE_CONTROL_KEY.format(task_id=task_id)
        if value in ("", "running", "resume"):
            await self.redis.delete(key)
        else:
            await self.redis.set(key, value, ex=ttl)

    async def get_pipeline_control(self, task_id: str) -> str:
        """Read the run-control flag; `running` when unset."""
        val = await self.redis.get(self.PIPELINE_CONTROL_KEY.format(task_id=task_id))
        return val or "running"

    async def list_pipeline_tasks(
        self,
        *,
        limit: int = 50,
        blueprint: str | None = None,
        repo: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return the most-recent pipeline tasks as dicts.

        Filters are AND-combined when both are passed (we intersect the
        sorted sets). When neither is given we return the global recent
        index. Newest first.
        """
        if blueprint and repo:
            ids = await self.redis.zinterstore(
                "_devai:pipeline:_tmpfilter",
                {
                    self.PIPELINE_BY_BLUEPRINT_KEY.format(blueprint=blueprint): 1.0,
                    self.PIPELINE_BY_REPO_KEY.format(repo=repo): 1.0,
                },
            )
            ids = await self.redis.zrevrange("_devai:pipeline:_tmpfilter", 0, limit - 1)
            await self.redis.delete("_devai:pipeline:_tmpfilter")
        elif blueprint:
            ids = await self.redis.zrevrange(self.PIPELINE_BY_BLUEPRINT_KEY.format(blueprint=blueprint), 0, limit - 1)
        elif repo:
            ids = await self.redis.zrevrange(self.PIPELINE_BY_REPO_KEY.format(repo=repo), 0, limit - 1)
        else:
            ids = await self.redis.zrevrange(self.PIPELINE_RECENT_KEY, 0, limit - 1)

        if not ids:
            return []

        # Multi-get the actual task payloads
        keys = [self.PIPELINE_TASK_KEY.format(task_id=tid) for tid in ids]
        values = await self.redis.mget(keys)
        return [json.loads(v) for v in values if v]

    async def delete_pipeline_task(self, task_id: str) -> None:
        """Remove a task from all indices."""
        task = await self.get_pipeline_task(task_id)
        pipe = self.redis.pipeline()
        pipe.delete(self.PIPELINE_TASK_KEY.format(task_id=task_id))
        pipe.zrem(self.PIPELINE_RECENT_KEY, task_id)
        if task:
            bp = task.get("blueprint")
            repo = task.get("repo")
            if bp:
                pipe.zrem(self.PIPELINE_BY_BLUEPRINT_KEY.format(blueprint=bp), task_id)
            if repo:
                pipe.zrem(self.PIPELINE_BY_REPO_KEY.format(repo=repo), task_id)
        await pipe.execute()

    # --- Cleanup ---

    async def close(self) -> None:
        """Close the Redis connection."""
        await self.redis.aclose()
