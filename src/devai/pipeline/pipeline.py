"""Pipeline — queue + executor + event emission.

Mirrors `internal/pipeline/pipeline.go` in Fiber. The Pipeline owns:

    - a queue of pending tasks (asyncio.Queue)
    - a concurrency limit (semaphore)
    - the BlueprintExecutor instance
    - the stage registry
    - a list of in-flight tasks (so the dashboard can list them)
    - an event-callback fan-out

It does NOT own persistence — that's the job of the StateManager (current
DevAI Redis/Postgres bridge). The pipeline calls `state_manager.persist_task`
after each transition; if state_manager is None it's a pure in-memory run
(useful for tests and the CLI).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from devai.adapters.workflow import create_workflow_adapter
from devai.blueprint.executor import BlueprintExecutor
from devai.blueprint.loader import Blueprint, discover_blueprints, load_blueprint
from devai.blueprint.registry import StageRegistry, register_defaults
from devai.pipeline.interfaces import StageDeps
from devai.pipeline.types import DevAITask, StageEvent, TaskState

logger = logging.getLogger(__name__)


EventCallback = Callable[[DevAITask, StageEvent], None]


class PipelineError(Exception):
    """Raised on bootstrap problems (missing blueprint, unknown stage, …)."""


class Pipeline:
    """Top-level orchestrator.

    Usage:
        deps = StageDeps(config=settings, scm=..., state_manager=...)
        pipe = Pipeline(deps, blueprint_dir="blueprints")
        await pipe.start()
        task_id = await pipe.submit(DevAITask(intent="...", blueprint="alm-pipeline"))
        await pipe.wait_for(task_id)
        await pipe.stop()
    """

    def __init__(
        self,
        deps: StageDeps,
        *,
        blueprint_dir: str | Path = "blueprints",
        registry: StageRegistry | None = None,
        concurrency: int = 4,
        default_stage_timeout: float = 900.0,
        event_callbacks: list[EventCallback] | None = None,
    ) -> None:
        self.deps = deps
        self._blueprints: dict[str, Blueprint] = {}
        self._blueprint_dir = Path(blueprint_dir)
        self._registry = registry or StageRegistry()
        if not self._registry.known_stages():
            register_defaults(self._registry)

        self._semaphore = asyncio.Semaphore(concurrency)
        self._tasks: dict[str, DevAITask] = {}
        self._task_done: dict[str, asyncio.Event] = {}
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._workers: list[asyncio.Task[None]] = []
        self._reaper: asyncio.Task[None] | None = None
        self._concurrency = concurrency
        self._stop_event = asyncio.Event()
        self._event_callbacks: list[EventCallback] = list(event_callbacks or [])

        # Durable work queue. Enabled when (a) a StateManager exposing the
        # reliable-queue API is wired and (b) config.pipeline_durable_queue is
        # on. In durable mode the queue lives in Redis so runs survive pod
        # rolls and any replica can execute them; otherwise we fall back to the
        # per-pod in-memory asyncio.Queue (tests, CLI, single-process dev).
        sm = deps.state_manager
        cfg = deps.config
        self._durable = bool(
            getattr(cfg, "pipeline_durable_queue", True) and sm is not None and hasattr(sm, "claim_next_task")
        )
        self._claim_ttl = int(getattr(cfg, "pipeline_claim_ttl", 180))
        self._reaper_interval = int(getattr(cfg, "pipeline_reaper_interval", 30))
        self._poll_interval = float(getattr(cfg, "pipeline_queue_poll_interval", 1.0))
        # Unique per pod (HOSTNAME is the pod name in K8s) + per Pipeline so the
        # reaper can distinguish this owner's live claims from a dead pod's.
        self._owner = f"{os.environ.get('HOSTNAME', 'local')}-{uuid.uuid4().hex[:6]}"

        self._executor = BlueprintExecutor(
            self._registry,
            deps,
            event_callback=self._fanout_event,
            default_stage_timeout=default_stage_timeout,
        )

        # Durable-execution seam. With workflow_provider=inproc (default) this
        # wraps the executor above and behaves identically; with =temporal it
        # routes runs through the generic BlueprintWorkflow. Blueprints/agents
        # are unaware of which backend is active.
        self._workflow_adapter = create_workflow_adapter(deps.config, executor=self._executor)

    # ── Public API ──────────────────────────────────────────────────

    def load_blueprints(self) -> None:
        """Discover every blueprint YAML under `blueprint_dir`.

        Idempotent — safe to call multiple times. Raises PipelineError if
        a blueprint references a stage that isn't registered.
        """
        bps = discover_blueprints(self._blueprint_dir)
        for bp in bps.values():
            for spec in bp.stages:
                if not self._registry.has(spec.stage):
                    raise PipelineError(
                        f"blueprint {bp.name!r} references unknown stage {spec.stage!r}. "
                        f"Known: {', '.join(self._registry.known_stages())}"
                    )
        self._blueprints = bps
        logger.info("loaded %d blueprints: %s", len(bps), sorted(bps))

    def add_blueprint(self, path: str | Path) -> Blueprint:
        return self.register_blueprint(load_blueprint(path))

    def register_blueprint(self, bp: Blueprint) -> Blueprint:
        """Register an already-loaded Blueprint into the live set.

        Used by the authoring service to make a user-authored blueprint
        runnable the instant it's created (no reload), mirroring how
        authored specializations hot-register. Validates that every stage
        the blueprint references is a known factory.
        """
        for spec in bp.stages:
            if not self._registry.has(spec.stage):
                raise PipelineError(f"blueprint {bp.name!r} references unknown stage {spec.stage!r}")
        self._blueprints[bp.name] = bp
        return bp

    def get_blueprint(self, name: str) -> Blueprint:
        if name not in self._blueprints:
            raise PipelineError(f"unknown blueprint {name!r}. Loaded: {sorted(self._blueprints)}")
        return self._blueprints[name]

    def list_blueprints(self) -> list[str]:
        return sorted(self._blueprints.keys())

    def add_event_callback(self, cb: EventCallback) -> None:
        self._event_callbacks.append(cb)

    async def start(self) -> None:
        """Spin up worker coroutines (+ the reaper in durable mode)."""
        if self._workers:
            return
        for i in range(self._concurrency):
            self._workers.append(asyncio.create_task(self._worker_loop(i), name=f"pipeline-worker-{i}"))
        if self._durable:
            self._reaper = asyncio.create_task(self._reaper_loop(), name="pipeline-reaper")
            logger.info(
                "pipeline durable queue ENABLED (owner=%s, %d workers, reaper@%ds, claim_ttl=%ds)",
                self._owner,
                self._concurrency,
                self._reaper_interval,
                self._claim_ttl,
            )
        else:
            logger.info("pipeline durable queue DISABLED — in-memory queue, %d workers", self._concurrency)

    async def stop(self) -> None:
        """Drain the queue and stop workers.

        In durable mode the queue is in Redis, so any task still executing here
        is simply left claimed — if this pod is going away the claim expires and
        the reaper on a surviving replica requeues it (resume-on-reclaim). We do
        NOT delete the durable queue; remaining ids persist for the next pod.
        """
        self._stop_event.set()
        if not self._durable:
            for _ in self._workers:
                self._queue.put_nowait("__STOP__")
        if self._reaper is not None:
            self._reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reaper
            self._reaper = None
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()

    async def submit(self, task: DevAITask) -> str:
        """Enqueue a task. Returns its id.

        Validates that the blueprint exists. The task starts in PENDING
        state; the worker transitions to QUEUED → ... when it picks up.
        """
        if task.blueprint not in self._blueprints:
            raise PipelineError(f"unknown blueprint {task.blueprint!r}")
        if not task.label:
            task.label = (task.intent[:60] or task.blueprint).strip()
        self._tasks[task.id] = task
        self._task_done[task.id] = asyncio.Event()
        if self._durable:
            # Persist the snapshot FIRST so whichever replica claims the id can
            # rebuild the task from Redis, then enqueue durably (idempotent).
            await self._persist(task)
            await self.deps.state_manager.enqueue_task(task.id)  # type: ignore[union-attr]
        else:
            await self._queue.put(task.id)
        logger.info("queued task %s blueprint=%s repo=%s", task.id, task.blueprint, task.repo)
        return task.id

    async def resubmit_persisted(self, task_dict: dict[str, Any]) -> str | None:
        """Re-enqueue a previously-persisted run (durable mode only).

        Used by the boot-time reconciler to pick up runs that were orphaned by
        a pod roll BEFORE the durable queue existed (or that fell through the
        reclaim gap). Idempotent — `enqueue_task` no-ops if the run is already
        active, so all replicas reconciling at once converge to one enqueue.
        Returns the task id if (re)enqueued, else None.
        """
        if not self._durable:
            return None
        task_id = task_dict.get("id")
        if not task_id:
            return None
        if task_dict.get("blueprint") not in self._blueprints:
            logger.warning("resubmit %s: unknown blueprint %r — skipped", task_id, task_dict.get("blueprint"))
            return None
        newly = await self.deps.state_manager.enqueue_task(task_id)  # type: ignore[union-attr]
        if newly:
            logger.info("reconcile: re-enqueued orphaned run %s (state=%s)", task_id, task_dict.get("state"))
            return task_id
        return None

    async def run_once(self, task: DevAITask) -> DevAITask:
        """Synchronous helper: submit, wait, return.

        Useful for the CLI and unit tests where we don't want to start
        the worker pool. Bypasses the queue and runs the blueprint inline.
        """
        if task.blueprint not in self._blueprints:
            raise PipelineError(f"unknown blueprint {task.blueprint!r}")
        if not task.label:
            task.label = (task.intent[:60] or task.blueprint).strip()
        self._tasks[task.id] = task
        self._task_done[task.id] = asyncio.Event()
        await self._execute_task(task)
        return task

    def get_task(self, task_id: str) -> DevAITask | None:
        return self._tasks.get(task_id)

    def list_tasks(self) -> list[DevAITask]:
        return list(self._tasks.values())

    def remove_task(self, task_id: str) -> bool:
        """Drop a task from the in-memory registry. Returns True if present."""
        return self._tasks.pop(task_id, None) is not None

    async def signal_run(self, task_id: str, signal_name: str, args: list[Any] | None = None) -> bool:
        """Send a control signal to a run via the workflow backend.

        Returns True only when the backend delivered it (Temporal). The
        in-process backends return False — control there is the Redis flag the
        executor polls — so callers set the flag AND signal, and one of the two
        is honored depending on the active provider.
        """
        return await self._workflow_adapter.signal(task_id, signal_name, args)

    async def wait_for(self, task_id: str, *, timeout: float | None = None) -> DevAITask:
        evt = self._task_done.get(task_id)
        if evt is None:
            raise PipelineError(f"unknown task {task_id}")
        try:
            await asyncio.wait_for(evt.wait(), timeout=timeout)
        except TimeoutError as e:
            raise PipelineError(f"timeout waiting for task {task_id}") from e
        return self._tasks[task_id]

    # ── Worker internals ────────────────────────────────────────────

    async def _worker_loop(self, worker_id: int) -> None:
        if self._durable:
            await self._durable_worker_loop(worker_id)
            return
        logger.debug("pipeline worker %d started", worker_id)
        while not self._stop_event.is_set():
            task_id = await self._queue.get()
            if task_id == "__STOP__":
                return
            task = self._tasks.get(task_id)
            if task is None:
                logger.warning("worker %d: unknown task id %s", worker_id, task_id)
                continue
            async with self._semaphore:
                try:
                    await self._execute_task(task)
                except Exception:  # noqa: BLE001
                    logger.exception("worker %d: task %s crashed", worker_id, task_id)

    async def _durable_worker_loop(self, worker_id: int) -> None:
        """Claim tasks from the Redis queue and execute them.

        BLMOVE gives exactly-once delivery across all replicas. The task is
        rebuilt from its Redis snapshot if it wasn't submitted on this pod. A
        heartbeat keeps the claim alive while running; `ack` clears it on exit
        (success, failure or cancel). If this pod dies mid-run the claim
        expires and the reaper on another replica requeues it — the executor
        skips completed stages, so the run resumes."""
        sm = self.deps.state_manager
        worker_name = f"{self._owner}-w{worker_id}"
        logger.debug("durable worker %s started", worker_name)
        while not self._stop_event.is_set():
            try:
                task_id = await sm.claim_next_task(  # type: ignore[union-attr]
                    worker_name, claim_ttl=self._claim_ttl
                )
            except Exception:  # noqa: BLE001 — Redis blip shouldn't kill the worker
                logger.exception("worker %s: claim_next_task failed", worker_name)
                await asyncio.sleep(self._poll_interval)
                continue
            if task_id is None:
                await asyncio.sleep(self._poll_interval)  # queue empty — poll again
                continue

            task = self._tasks.get(task_id)
            if task is None:
                snapshot = await self._load_snapshot(task_id)
                if snapshot is None:
                    logger.warning("worker %s: no snapshot for %s — dropping", worker_name, task_id)
                    await sm.ack_task(task_id)  # type: ignore[union-attr]
                    continue
                task = DevAITask.from_dict(snapshot)
                self._tasks[task.id] = task
                self._task_done.setdefault(task.id, asyncio.Event())

            if task.is_terminal:
                # Already finished (e.g. acked elsewhere) — clear and move on.
                await sm.ack_task(task_id)  # type: ignore[union-attr]
                continue

            hb = asyncio.create_task(self._heartbeat(task_id, worker_name), name=f"hb-{task_id}")
            try:
                async with self._semaphore:
                    await self._execute_task(task)
            except Exception:  # noqa: BLE001
                logger.exception("worker %s: task %s crashed", worker_name, task_id)
            finally:
                hb.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await hb
                with contextlib.suppress(Exception):
                    await sm.ack_task(task_id)  # type: ignore[union-attr]

    async def _heartbeat(self, task_id: str, worker_name: str) -> None:
        """Refresh the task's liveness claim while it executes, at a quarter of
        the TTL so a transient stall doesn't trip a false reclaim."""
        interval = max(5.0, self._claim_ttl / 4.0)
        sm = self.deps.state_manager
        while True:
            await asyncio.sleep(interval)
            with contextlib.suppress(Exception):
                await sm.heartbeat_task(task_id, worker_name, claim_ttl=self._claim_ttl)  # type: ignore[union-attr]

    async def _reaper_loop(self) -> None:
        """Periodically requeue tasks whose owning pod died (claim expired)."""
        sm = self.deps.state_manager
        while not self._stop_event.is_set():
            try:
                await asyncio.sleep(self._reaper_interval)
                reclaimed = await sm.reclaim_stale_tasks()  # type: ignore[union-attr]
                if reclaimed:
                    logger.warning("reaper requeued %d orphaned run(s): %s", len(reclaimed), reclaimed)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("reaper sweep failed")

    async def _load_snapshot(self, task_id: str) -> dict[str, Any] | None:
        sm = self.deps.state_manager
        getter = getattr(sm, "get_pipeline_task", None)
        if getter is None:
            return None
        with contextlib.suppress(Exception):
            return await getter(task_id)
        return None

    async def _execute_task(self, task: DevAITask) -> None:
        blueprint = self._blueprints[task.blueprint]
        task.transition(TaskState.QUEUED)
        try:
            await self._workflow_adapter.run_blueprint(blueprint, task)
        finally:
            await self._persist(task)
            evt = self._task_done.get(task.id)
            if evt is not None:
                evt.set()

    async def _persist(self, task: DevAITask) -> None:
        sm = self.deps.state_manager
        if sm is None:
            return
        persist_fn = getattr(sm, "persist_task", None) or getattr(sm, "save_task", None)
        if persist_fn is None:
            return
        try:
            maybe = persist_fn(task.to_dict())
            if asyncio.iscoroutine(maybe):
                await maybe
        except Exception:  # noqa: BLE001
            logger.exception("state_manager.persist_task failed")

    def _fanout_event(self, task: DevAITask, event: StageEvent) -> None:
        for cb in self._event_callbacks:
            try:
                cb(task, event)
            except Exception:  # noqa: BLE001
                logger.exception("event callback raised")


__all__ = ["EventCallback", "Pipeline", "PipelineError"]
