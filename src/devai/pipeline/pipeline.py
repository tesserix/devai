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
import logging
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
        self._concurrency = concurrency
        self._stop_event = asyncio.Event()
        self._event_callbacks: list[EventCallback] = list(event_callbacks or [])

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
        self._workflow_adapter = create_workflow_adapter(
            deps.config, executor=self._executor
        )

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
        bp = load_blueprint(path)
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
        """Spin up worker coroutines."""
        if self._workers:
            return
        for i in range(self._concurrency):
            self._workers.append(asyncio.create_task(self._worker_loop(i), name=f"pipeline-worker-{i}"))

    async def stop(self) -> None:
        """Drain the queue and stop workers."""
        self._stop_event.set()
        for _ in self._workers:
            self._queue.put_nowait("__STOP__")
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
        await self._queue.put(task.id)
        logger.info("queued task %s blueprint=%s repo=%s", task.id, task.blueprint, task.repo)
        return task.id

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

    async def signal_run(
        self, task_id: str, signal_name: str, args: list[Any] | None = None
    ) -> bool:
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
        except asyncio.TimeoutError as e:
            raise PipelineError(f"timeout waiting for task {task_id}") from e
        return self._tasks[task_id]

    # ── Worker internals ────────────────────────────────────────────

    async def _worker_loop(self, worker_id: int) -> None:
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
