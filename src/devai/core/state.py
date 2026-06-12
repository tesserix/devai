"""Redis-backed state manager for pipeline run tracking."""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

import redis.asyncio as redis
from redis.exceptions import WatchError

from devai.models import AgentResult, PipelineContext

logger = logging.getLogger(__name__)

# Terminal task states — persist_task refuses to overwrite a terminal snapshot
# with a non-terminal one (zombie-writer protection). Sourced from the pipeline
# state machine; falls back to the literal set in slim environments.
try:
    from devai.pipeline.types import TERMINAL_STATES as _PIPELINE_TERMINAL_STATES

    _TERMINAL_STATE_VALUES = frozenset(s.value for s in _PIPELINE_TERMINAL_STATES)
except Exception:  # noqa: BLE001
    _TERMINAL_STATE_VALUES = frozenset(
        {"completed", "failed", "stage_failed", "agent_timeout", "budget_exceeded", "cancelled"}
    )


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

    async def delete_run(self, run_id: str) -> None:
        """Remove a legacy orchestrator run + its side keys and indices.

        Used by the dashboard's run-delete action to clean up old / zombie runs
        (the Fleet list reads this `devai:run:*` store). Idempotent."""
        data = await self.redis.hgetall(f"devai:run:{run_id}")
        repo = data.get("repo") if data else None
        pipe = self.redis.pipeline()
        pipe.delete(f"devai:run:{run_id}")
        pipe.delete(f"devai:run:{run_id}:agents")
        pipe.delete(f"devai:run:{run_id}:events")
        pipe.delete(f"devai:run:{run_id}:a2a_messages")
        pipe.lrem("devai:runs:recent", 0, run_id)
        pipe.zrem("devai:runs:by_time", run_id)
        if repo:
            pipe.lrem(f"devai:runs:by_repo:{repo}", 0, run_id)
        await pipe.execute()

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

    # --- Durable work queue (reliable-queue pattern) ---
    #
    # The pipeline's work queue lives in Redis, NOT in any single pod's RAM,
    # so a task survives pod rolls / scale events and is executed by whichever
    # replica claims it. Three structures cooperate:
    #
    #   QUEUE      (LIST)  — task ids waiting to be picked up (LPUSH head,
    #                        BLMOVE from tail → FIFO).
    #   PROCESSING (LIST)  — task ids a worker has claimed and is executing.
    #   ACTIVE     (SET)   — task ids currently queued-or-processing; the
    #                        SADD guard makes enqueue idempotent so the same
    #                        run is never queued twice (3 replicas reconciling
    #                        on boot all converge to one enqueue).
    #   CLAIM:{id} (STRING+TTL) — liveness heartbeat of the owning worker.
    #                        Refreshed while executing; if it expires the
    #                        owner died and the reaper moves the id back to
    #                        QUEUE. The executor skips completed stages on the
    #                        re-run, so reclaim resumes rather than duplicates.
    PIPELINE_QUEUE_KEY = "devai:pipeline:queue"
    PIPELINE_PROCESSING_KEY = "devai:pipeline:processing"
    PIPELINE_ACTIVE_KEY = "devai:pipeline:active"
    PIPELINE_CLAIM_KEY = "devai:pipeline:claim:{task_id}"

    async def persist_task(
        self, task_dict: dict[str, Any], *, ttl: int | None = None, force: bool = False
    ) -> None:
        """Write a DevAITask dict to Redis, last-writer-wins by `updated_at`.

        The Pipeline persists after every state mutation, but some of those
        writes are fire-and-forget (the per-stage event callback schedules a
        persist of the snapshot captured at emit time). The executor emits the
        final stage event *before* the terminal transition, so a late-landing
        event persist could otherwise clobber a finished run's snapshot back to
        a running state — and the boot reconciler would then resurrect a run
        that already completed/failed. Guard with an optimistic WATCH/MULTI on
        the snapshot key: a persist whose `updated_at` is older than what's
        already stored is dropped, so the newest mutation always wins
        regardless of the order persists actually land. The recent /
        by_blueprint / by_repo indices are updated only when we write.

        ``force=True`` bypasses BOTH guards — used exclusively by the
        deliberate resume-from-failure path, which legitimately moves a
        terminal run back to ``queued``.
        """
        task_id = task_dict.get("id")
        if not task_id:
            logger.warning("persist_task: dict missing 'id' — ignored")
            return

        blueprint = task_dict.get("blueprint", "unknown")
        repo = task_dict.get("repo", "")
        ttl = ttl if ttl is not None else self.result_ttl
        incoming_ts = float(task_dict.get("updated_at") or 0.0)
        payload = json.dumps(task_dict)
        key = self.PIPELINE_TASK_KEY.format(task_id=task_id)

        async with self.redis.pipeline() as pipe:
            while True:
                try:
                    await pipe.watch(key)
                    current = await pipe.get(key)  # immediate read under WATCH
                    if current and not force:
                        try:
                            stored = json.loads(current)
                            stored_ts = float(stored.get("updated_at") or 0.0)
                            stored_state = str(stored.get("state") or "")
                        except (ValueError, TypeError):
                            stored_ts, stored_state = 0.0, ""
                        if incoming_ts < stored_ts:
                            await pipe.unwatch()
                            return  # stale — a newer snapshot already stored
                        # Terminal beats non-terminal REGARDLESS of timestamps.
                        # updated_at ordering alone can't stop a zombie writer:
                        # during a pod roll the draining pod keeps executing
                        # (and persisting newer 'running' snapshots) AFTER the
                        # new pod cancelled the run — the reaper then requeued
                        # and resurrected a stopped run. Once a run is terminal,
                        # only another terminal snapshot may replace it.
                        incoming_state = str(task_dict.get("state") or "")
                        if stored_state in _TERMINAL_STATE_VALUES and incoming_state not in _TERMINAL_STATE_VALUES:
                            await pipe.unwatch()
                            logger.info(
                                "persist_task: dropping non-terminal write (%s) over terminal snapshot (%s) for %s",
                                incoming_state,
                                stored_state,
                                task_id,
                            )
                            return
                    now = time.time()
                    pipe.multi()
                    pipe.set(key, payload, ex=ttl)
                    # Sorted set keyed by updated_at so we can list "most recent"
                    pipe.zadd(self.PIPELINE_RECENT_KEY, {task_id: task_dict.get("updated_at", now)})
                    # Cap the recent index at 1000 entries
                    pipe.zremrangebyrank(self.PIPELINE_RECENT_KEY, 0, -1001)
                    if blueprint:
                        pipe.zadd(self.PIPELINE_BY_BLUEPRINT_KEY.format(blueprint=blueprint), {task_id: now})
                    if repo:
                        pipe.zadd(self.PIPELINE_BY_REPO_KEY.format(repo=repo), {task_id: now})
                    await pipe.execute()
                    return
                except WatchError:
                    continue  # snapshot changed under us — re-read and retry

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
            # Per-call temp key so concurrent filtered lists can't clobber or
            # leak each other's intersection results.
            tmp_key = f"_devai:pipeline:_tmpfilter:{uuid.uuid4().hex}"
            try:
                await self.redis.zinterstore(
                    tmp_key,
                    {
                        self.PIPELINE_BY_BLUEPRINT_KEY.format(blueprint=blueprint): 1.0,
                        self.PIPELINE_BY_REPO_KEY.format(repo=repo): 1.0,
                    },
                )
                ids = await self.redis.zrevrange(tmp_key, 0, limit - 1)
            finally:
                await self.redis.delete(tmp_key)
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

    async def enqueue_task(self, task_id: str) -> bool:
        """Add a task to the durable work queue. Idempotent.

        Returns True if the task was newly enqueued, False if it was already
        active (queued or processing). The SADD guard is what makes this safe
        to call from every replica on boot — only the first call enqueues.
        """
        added = await self.redis.sadd(self.PIPELINE_ACTIVE_KEY, task_id)
        if added:
            await self.redis.lpush(self.PIPELINE_QUEUE_KEY, task_id)
        return bool(added)

    async def claim_next_task(self, worker_id: str, *, claim_ttl: int = 180) -> str | None:
        """Atomically move the next queued task to the processing list and
        stamp a liveness claim. Returns the id, or None if the queue is empty.

        Non-blocking on purpose: a *blocking* BLMOVE conflicts with the shared
        client's socket read timeout (the read times out mid-block and raises
        redis.TimeoutError), so workers poll with this instead. LMOVE is still
        atomic — exactly one worker across all replicas gets a given id. The
        claim key (refreshed by `heartbeat_task`) lets the reaper tell a live
        owner from a dead one.
        """
        task_id = await self.redis.lmove(
            self.PIPELINE_QUEUE_KEY,
            self.PIPELINE_PROCESSING_KEY,
            "RIGHT",
            "LEFT",
        )
        if task_id is None:
            return None
        await self.redis.set(self.PIPELINE_CLAIM_KEY.format(task_id=task_id), worker_id, ex=claim_ttl)
        return task_id

    async def heartbeat_task(self, task_id: str, worker_id: str, *, claim_ttl: int = 180) -> None:
        """Refresh the liveness claim on a task we're executing. Called on a
        timer so a long-running (but alive) stage isn't falsely reclaimed."""
        await self.redis.set(self.PIPELINE_CLAIM_KEY.format(task_id=task_id), worker_id, ex=claim_ttl)

    async def ack_task(self, task_id: str) -> None:
        """Mark a task done: drop it from processing, active and clear its
        claim. Called whether the run completed, failed or was cancelled — the
        task's terminal state is recorded separately in its snapshot."""
        pipe = self.redis.pipeline()
        pipe.lrem(self.PIPELINE_PROCESSING_KEY, 0, task_id)
        pipe.srem(self.PIPELINE_ACTIVE_KEY, task_id)
        pipe.delete(self.PIPELINE_CLAIM_KEY.format(task_id=task_id))
        await pipe.execute()

    async def release_task(self, task_id: str) -> int:
        """Hand a claimed task back to the queue UNTOUCHED — for a worker
        that claimed something it can't execute. The durable queue is shared
        by every PipelineService in the cluster (devai-api AND devai-sre),
        and each loads a different blueprint set; without a release path the
        wrong service crashes on the claim, acks it, and strands the run in
        `pending` forever.

        Keeps the task in the active set, requeues at the BACK so another
        service gets first crack, clears this worker's claim. Returns the
        cumulative release count so callers can fail tasks that NO live
        service can run instead of ping-ponging them forever."""
        released_key = f"devai:pipeline:released:{task_id}"
        pipe = self.redis.pipeline()
        pipe.lrem(self.PIPELINE_PROCESSING_KEY, 0, task_id)
        pipe.lpush(self.PIPELINE_QUEUE_KEY, task_id)
        pipe.delete(self.PIPELINE_CLAIM_KEY.format(task_id=task_id))
        pipe.incr(released_key)
        pipe.expire(released_key, 3600)
        results = await pipe.execute()
        try:
            return int(results[3])
        except (IndexError, ValueError, TypeError):
            return 0

    async def reclaim_stale_tasks(self) -> list[str]:
        """Move every processing task whose owner died (claim expired) back to
        the queue so a live worker picks it up. Returns the reclaimed ids.

        Race-safe across replicas: `LREM` is atomic and returns the count it
        removed, so for a given id exactly one reaper sees count>0 and requeues
        it; the others see 0 and skip. The id stays in the ACTIVE set the whole
        time (it's being re-run, not newly added)."""
        ids = await self.redis.lrange(self.PIPELINE_PROCESSING_KEY, 0, -1)
        reclaimed: list[str] = []
        for task_id in ids:
            if await self.redis.exists(self.PIPELINE_CLAIM_KEY.format(task_id=task_id)):
                continue  # owner still alive
            removed = await self.redis.lrem(self.PIPELINE_PROCESSING_KEY, 0, task_id)
            if removed:
                await self.redis.lpush(self.PIPELINE_QUEUE_KEY, task_id)
                reclaimed.append(task_id)
        return reclaimed

    async def is_task_active(self, task_id: str) -> bool:
        """True if the task is currently queued or processing."""
        return bool(await self.redis.sismember(self.PIPELINE_ACTIVE_KEY, task_id))

    # --- Cleanup ---

    async def close(self) -> None:
        """Close the Redis connection."""
        await self.redis.aclose()
