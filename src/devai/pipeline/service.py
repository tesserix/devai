"""PipelineService — FastAPI-side wrapper around `Pipeline`.

`Pipeline` itself is a queue + executor. PipelineService is what the rest
of the codebase actually talks to:

- Start/stop with FastAPI's lifespan.
- Hand out a dispatch surface (`dispatch(intent=..., blueprint=...)`).
- Maintain a bounded ring buffer of stage events for SSE replay.
- Mirror state mutations into StateManager.persist_task for durability.
- Expose helpers (list_blueprints, list_stages, list_tasks, get_task).

Stored on `app.state.pipeline_service`. Webhook routes, the SRE server,
the chat agent, and the dashboard all read from the same instance.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from devai.pipeline.interfaces import StageDeps
from devai.pipeline.types import (
    FAILURE_STATES,
    TERMINAL_STATES,
    DevAITask,
    StageEvent,
    StageEventPhase,
    TaskState,
)

if TYPE_CHECKING:
    from devai.adapters.event_bus.base import EventBusAdapter
    from devai.config import Settings
    from devai.core.event_bus import EventBus
    from devai.core.state import StateManager
    from devai.pipeline.pipeline import Pipeline
    from devai.scm.base import SCMClient

logger = logging.getLogger(__name__)


class PipelineService:
    """Operational facade over `Pipeline`.

    Lifecycle:
        svc = PipelineService(config, scm, state_manager, event_bus)
        await svc.start()
        ...
        task_id = await svc.dispatch(intent="...", blueprint="alm-pipeline", repo="org/repo")
        ...
        await svc.stop()
    """

    def __init__(
        self,
        config: Settings,
        *,
        scm: SCMClient | None = None,
        state_manager: StateManager | None = None,
        event_bus: EventBus | None = None,
        event_bus_adapter: EventBusAdapter | None = None,
        blueprint_dir: str | Path | None = None,
    ) -> None:
        self.config = config
        self.scm = scm
        self.state_manager = state_manager
        self.event_bus = event_bus
        self.event_bus_adapter = event_bus_adapter
        self.blueprint_dir = Path(blueprint_dir or config.pipeline_blueprint_dir)

        self._pipeline: Pipeline | None = None
        self._started = False
        self._owns_event_bus_adapter = False  # True when we constructed it
        self._ring: deque[tuple[float, str, dict[str, Any]]] = deque(
            maxlen=getattr(config, "pipeline_event_ring_size", 1000)
        )
        self._sse_queues: list[asyncio.Queue[tuple[float, str, dict[str, Any]]]] = []
        self._lock = asyncio.Lock()

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def start(self) -> None:
        """Build the Pipeline, load blueprints, start workers.

        Safe to call twice — second call is a no-op.
        """
        if self._started:
            return

        from devai.adapters.event_bus import create_event_bus_adapter
        from devai.adapters.llm import create_llm_adapter
        from devai.adapters.memory import create_memory_adapter
        from devai.pipeline.pipeline import Pipeline  # local import to avoid cycle

        # Make the StateManager / Database resolvable by the memory factory
        # without them needing their own env vars. The factory looks at
        # `settings.state_manager` and `settings.database` first.
        try:
            if self.state_manager is not None and not hasattr(self.config, "state_manager"):
                object.__setattr__(self.config, "state_manager", self.state_manager)  # type: ignore[arg-type]
        except (TypeError, AttributeError):
            # Pydantic Settings is frozen by default — fall through; the
            # factory will use `settings.redis_url` instead.
            pass

        memory_adapter = create_memory_adapter(self.config)
        self._memory_adapter = memory_adapter
        llm_adapter = create_llm_adapter(self.config)
        self._llm_adapter = llm_adapter

        # Event-bus adapter — built here and connected immediately so
        # `_on_event` can publish stage events to NATS without needing
        # to lazy-init on the hot path. If the caller already injected
        # an adapter (test path), we use that one and don't take
        # ownership of close().
        if self.event_bus_adapter is None:
            self.event_bus_adapter = create_event_bus_adapter(self.config)
            self._owns_event_bus_adapter = True
            try:
                await self.event_bus_adapter.connect()
                logger.info(
                    "PipelineService event-bus connected: provider=%s",
                    self.event_bus_adapter.provider_name,
                )
            except Exception:
                # Factory's create_and_connect is what we'd normally call,
                # but we wanted ownership tracking. Degrade to noop on
                # connect failure rather than crashing the pipeline.
                logger.exception(
                    "PipelineService event-bus connect failed (%s) — degrading to in-process noop",
                    self.event_bus_adapter.provider_name,
                )
                from devai.adapters.event_bus.noop import NoopEventBusAdapter

                self.event_bus_adapter = NoopEventBusAdapter()
                await self.event_bus_adapter.connect()

        # Load the YAML specialization catalog once at startup and hand
        # the registry to stages via StageDeps.extra. The run_specialization
        # stage uses it to resolve names like 'senior_developer' to the
        # parsed spec without re-reading the disk on every call.
        spec_registry = None
        if getattr(self.config, "specializations_enabled", True):
            try:
                from devai.specializations.registry import SpecializationRegistry

                spec_registry = SpecializationRegistry.from_directory(
                    getattr(self.config, "specializations_dir", "specializations")
                )
                logger.info(
                    "Pipeline loaded %d specializations from %s",
                    len(spec_registry),
                    getattr(self.config, "specializations_dir", "specializations"),
                )
            except Exception:
                logger.exception("Specialization registry load failed — run_specialization will degrade to stubs")

        # ── K8s Job runtime ────────────────────────────────────────────
        # When enabled (DEVAI_K8S_RUNTIME_ENABLED=true), every stage that
        # uses `stage: run_as_job` dispatches a Pod and waits via the
        # JobWatcher. When disabled, the runtime is None and JobRunnerStage
        # falls back to a stub — the pipeline keeps running, just inline.
        k8s_runtime = None
        job_watcher = None
        registry_client = getattr(self, "registry_client", None)
        try:
            from devai.runtime import JobWatcher, create_runtime

            k8s_runtime = await create_runtime(self.config)
            if k8s_runtime is not None:
                job_watcher = JobWatcher(k8s_runtime)
                await job_watcher.start()
                logger.info(
                    "PipelineService: k8s runtime active (namespace=%s, runner=%s)",
                    k8s_runtime.config.namespace,
                    k8s_runtime.config.runner_image,
                )
        except Exception:  # noqa: BLE001
            logger.exception("PipelineService: k8s runtime init failed — continuing without it")
            k8s_runtime = None
            job_watcher = None
        self._k8s_runtime = k8s_runtime
        self._job_watcher = job_watcher

        extra: dict[str, Any] = {}
        if spec_registry is not None:
            extra["specialization_registry"] = spec_registry
        if k8s_runtime is not None:
            extra["k8s_runtime"] = k8s_runtime
        if job_watcher is not None:
            extra["job_watcher"] = job_watcher
        if registry_client is not None:
            extra["registry_client"] = registry_client

        deps = StageDeps(
            config=self.config,
            scm=self.scm,
            state_manager=self.state_manager,
            event_bus=self.event_bus,
            memory=memory_adapter,
            llm=llm_adapter,
            event_bus_adapter=self.event_bus_adapter,
            extra=extra or None,
        )
        self._pipeline = Pipeline(
            deps,
            blueprint_dir=self.blueprint_dir,
            concurrency=getattr(self.config, "pipeline_concurrency", 4),
            default_stage_timeout=float(getattr(self.config, "pipeline_default_stage_timeout", 900)),
        )
        self._pipeline.add_event_callback(self._on_event)

        try:
            self._pipeline.load_blueprints()
        except Exception as e:  # noqa: BLE001 — surface but don't block startup
            logger.error("pipeline blueprint load failed: %s", e)

        await self._pipeline.start()
        self._started = True
        logger.info(
            "PipelineService started — %d blueprints, %d stages",
            len(self._pipeline.list_blueprints()),
            len(self._pipeline._registry.known_stages()),  # noqa: SLF001
        )

    async def stop(self) -> None:
        if not self._started or self._pipeline is None:
            return
        await self._pipeline.stop()
        # Memory adapters that own connections (mem0 HTTP client, zep HTTP
        # client) clean up here. Adapters that share a connection with the
        # host (Redis, pgvector) implement close() as a no-op.
        memory = getattr(self, "_memory_adapter", None)
        if memory is not None:
            try:
                await memory.close()
            except Exception:  # noqa: BLE001
                logger.exception("memory adapter close failed")
        llm = getattr(self, "_llm_adapter", None)
        if llm is not None:
            try:
                await llm.close()
            except Exception:  # noqa: BLE001
                logger.exception("llm adapter close failed")
        if self._owns_event_bus_adapter and self.event_bus_adapter is not None:
            try:
                await self.event_bus_adapter.close()
            except Exception:  # noqa: BLE001
                logger.exception("event_bus adapter close failed")
        watcher = getattr(self, "_job_watcher", None)
        if watcher is not None:
            try:
                await watcher.stop()
            except Exception:  # noqa: BLE001
                logger.exception("JobWatcher stop failed")
        runtime = getattr(self, "_k8s_runtime", None)
        if runtime is not None:
            try:
                await runtime.close()
            except Exception:  # noqa: BLE001
                logger.exception("K8sJobRuntime close failed")
        self._started = False
        logger.info("PipelineService stopped")

    # ── Dispatch surface ─────────────────────────────────────────────

    async def dispatch(
        self,
        *,
        intent: str,
        blueprint: str | None = None,
        repo: str = "",
        trigger_type: str = "manual",
        label: str = "",
        agent_context: dict[str, Any] | None = None,
        principal: dict[str, Any] | None = None,
        trace_id: str | None = None,
    ) -> str:
        """Enqueue a new task. Returns its id.

        Returns the task id even if the blueprint is unknown — but in that
        case raises PipelineError. Callers should treat this as create+enqueue.

        ``principal`` / ``trace_id`` are stamped onto the DevAITask so the
        executor, every stage, and the persisted record all know which
        human triggered the work. Both are optional — passing None is the
        same as system-triggered (e.g. SRE cron).
        """
        self._ensure_started()
        assert self._pipeline is not None  # for type-checker

        bp = blueprint or self.config.pipeline_default_blueprint
        task = DevAITask(
            intent=intent,
            blueprint=bp,
            repo=repo,
            trigger_type=trigger_type,
            label=label,
            principal=dict(principal) if principal else None,
            trace_id=trace_id,
            triggered_by=(principal.get("email") if principal else None),
        )
        if agent_context:
            task.agent_context.update(agent_context)
        # Also stash identity into agent_context so stages that don't
        # know about the new task fields can still read it from the
        # handover bag (which is the documented stage contract).
        if principal:
            task.agent_context.setdefault("principal", dict(principal))
        if trace_id:
            task.agent_context.setdefault("trace_id", trace_id)
        await self._pipeline.submit(task)
        await self._publish_task_event("created", task)
        return task.id

    async def run_once(
        self,
        *,
        intent: str,
        blueprint: str | None = None,
        repo: str = "",
        trigger_type: str = "manual",
        agent_context: dict[str, Any] | None = None,
        principal: dict[str, Any] | None = None,
        trace_id: str | None = None,
    ) -> DevAITask:
        """Synchronous dispatch — submit, wait, return the final task.

        Used by the SRE scanner where we want to await completion inline.
        """
        self._ensure_started()
        assert self._pipeline is not None

        bp = blueprint or self.config.pipeline_default_blueprint
        task = DevAITask(
            intent=intent,
            blueprint=bp,
            repo=repo,
            trigger_type=trigger_type,
            principal=dict(principal) if principal else None,
            trace_id=trace_id,
            triggered_by=(principal.get("email") if principal else None),
        )
        if agent_context:
            task.agent_context.update(agent_context)
        if principal:
            task.agent_context.setdefault("principal", dict(principal))
        if trace_id:
            task.agent_context.setdefault("trace_id", trace_id)
        await self._publish_task_event("created", task)
        result = await self._pipeline.run_once(task)
        # run_once is synchronous from the caller's POV, so emit a terminal
        # event here too — the stage-event path also fires, but a
        # subscriber that's listening for task.* without subject wildcards
        # gets a definitive end-of-run signal.
        terminal_kind = (
            "failed" if result.state in FAILURE_STATES
            else "completed" if result.state == TaskState.COMPLETED
            else None
        )
        if terminal_kind:
            await self._publish_task_event(terminal_kind, result)
        return result

    # ── Read surface ─────────────────────────────────────────────────

    def list_blueprints(self) -> list[dict[str, Any]]:
        if self._pipeline is None:
            return []
        out = []
        for name in self._pipeline.list_blueprints():
            bp = self._pipeline.get_blueprint(name)
            out.append(
                {
                    "name": bp.name,
                    "description": bp.description,
                    "stage_count": len(bp.stages),
                    "metadata": dict(bp.metadata),
                }
            )
        return out

    def list_stage_keys(self) -> list[str]:
        if self._pipeline is None:
            return []
        return self._pipeline._registry.known_stages()  # noqa: SLF001

    def list_tasks_in_memory(self) -> list[dict[str, Any]]:
        """In-memory snapshot — tasks currently tracked by the running
        Pipeline. For historical tasks across restarts, use
        StateManager.list_pipeline_tasks() (Redis-backed)."""
        if self._pipeline is None:
            return []
        return [t.to_dict() for t in self._pipeline.list_tasks()]

    def get_task_in_memory(self, task_id: str) -> dict[str, Any] | None:
        if self._pipeline is None:
            return None
        task = self._pipeline.get_task(task_id)
        return task.to_dict() if task is not None else None

    async def list_persisted_tasks(
        self, *, limit: int = 50, blueprint: str | None = None, repo: str | None = None
    ) -> list[dict[str, Any]]:
        if self.state_manager is None:
            return []
        return await self.state_manager.list_pipeline_tasks(limit=limit, blueprint=blueprint, repo=repo)

    async def get_persisted_task(self, task_id: str) -> dict[str, Any] | None:
        if self.state_manager is None:
            return None
        return await self.state_manager.get_pipeline_task(task_id)

    async def get_task(self, task_id: str) -> dict[str, Any] | None:
        """Best-effort: prefer the in-memory snapshot, fall back to Redis."""
        in_mem = self.get_task_in_memory(task_id)
        if in_mem is not None:
            return in_mem
        return await self.get_persisted_task(task_id)

    # ── SSE event stream ────────────────────────────────────────────

    def recent_events(self, limit: int = 200) -> list[dict[str, Any]]:
        return [
            {"timestamp": ts, "task_id": tid, **payload}
            for ts, tid, payload in list(self._ring)[-limit:]
        ]

    async def event_stream(
        self, *, replay: int = 0
    ) -> AsyncIterator[tuple[float, str, dict[str, Any]]]:
        """Async generator yielding live stage events.

        `replay` — emit the last N events from the ring buffer before
        switching to live. Useful for SSE reconnects with Last-Event-ID.
        """
        queue: asyncio.Queue[tuple[float, str, dict[str, Any]]] = asyncio.Queue()
        async with self._lock:
            self._sse_queues.append(queue)

        # Replay first
        if replay:
            for entry in list(self._ring)[-replay:]:
                yield entry

        try:
            while True:
                yield await queue.get()
        finally:
            async with self._lock:
                if queue in self._sse_queues:
                    self._sse_queues.remove(queue)

    # ── Internal: per-event fan-out ──────────────────────────────────

    def _on_event(self, task: DevAITask, event: StageEvent) -> None:
        """Called by Pipeline / BlueprintExecutor for every stage event.

        We do four things:
          1. Append to the ring buffer for SSE replay.
          2. Push to every subscribed SSE queue.
          3. Schedule a persist_task to Redis (best-effort, fire-and-forget).
          4. Publish to NATS via the event-bus adapter so any external
             subscriber (legacy agents, dashboards, downstream services)
             sees stage transitions and terminal state changes.
        """
        ts = event.timestamp
        payload = {
            "stage": event.stage,
            "phase": event.phase.value,
            "duration_ms": event.duration_ms,
            "message": event.message,
            "error": event.error,
            "task_state": task.state.value,
            "blueprint": task.blueprint,
            "repo": task.repo,
        }
        self._ring.append((ts, task.id, payload))

        for q in list(self._sse_queues):
            try:
                q.put_nowait((ts, task.id, payload))
            except asyncio.QueueFull:
                logger.warning("SSE queue full — dropping event")

        if self.state_manager is not None:
            try:
                asyncio.create_task(
                    self.state_manager.persist_task(task.to_dict(), ttl=self.config.pipeline_task_ttl)
                )
            except RuntimeError:
                # No running loop (test context) — skip persistence.
                pass

        # Mirror to NATS. Subject layout:
        #   devai.pipeline.stage.<stage>.<phase>   per-stage progress
        #   devai.pipeline.task.<completed|failed> on terminal transitions
        # Failure-mode: NATS goes down → publish raises → we log + carry on.
        # The pipeline must never crash because the broker is gone.
        try:
            self._schedule_publish(
                f"devai.pipeline.stage.{event.stage}.{event.phase.value}",
                {"task_id": task.id, **payload, "timestamp": ts},
            )
        except RuntimeError:
            pass  # no running loop

        if task.state in TERMINAL_STATES and event.phase in (
            StageEventPhase.COMPLETED,
            StageEventPhase.FAILED,
        ):
            kind = "failed" if task.state in FAILURE_STATES else "completed"
            try:
                self._schedule_publish(
                    f"devai.pipeline.task.{kind}",
                    self._task_event_payload(kind, task),
                )
            except RuntimeError:
                pass

    # ── Internal: NATS publish helpers ───────────────────────────────

    def _task_event_payload(self, kind: str, task: DevAITask) -> dict[str, Any]:
        return {
            "kind": kind,
            "task_id": task.id,
            "blueprint": task.blueprint,
            "repo": task.repo,
            "intent": task.intent,
            "trigger_type": task.trigger_type,
            "state": task.state.value,
            "current_stage": task.current_stage,
            "stages_completed": list(task.stages_completed),
            "stages_failed": list(task.stages_failed),
            "error": task.error,
            "failed_stage": task.failed_stage,
            "pr_number": task.pr_number,
            "issue_number": task.issue_number,
            "branch_name": task.branch_name,
            "triggered_by": task.triggered_by,
            "trace_id": task.trace_id,
            "preview_url": task.agent_context.get("preview_url"),
            "editor_url": task.agent_context.get("editor_url"),
            "timestamp": time.time(),
        }

    async def _publish_task_event(self, kind: str, task: DevAITask) -> None:
        """Publish a task-level event (`created`, `completed`, `failed`)."""
        adapter = self.event_bus_adapter
        if adapter is None:
            return
        try:
            await adapter.publish(
                f"devai.pipeline.task.{kind}",
                self._task_event_payload(kind, task),
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "event_bus publish failed: subject=devai.pipeline.task.%s task=%s",
                kind,
                task.id,
                exc_info=True,
            )

    def _schedule_publish(self, subject: str, payload: dict[str, Any]) -> None:
        """Fire-and-forget publish from sync context (the _on_event hook
        is sync). Schedules a task on the running loop; raises RuntimeError
        when called outside one so the caller can no-op."""
        adapter = self.event_bus_adapter
        if adapter is None:
            return

        async def _pub() -> None:
            try:
                await adapter.publish(subject, payload)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "event_bus publish failed: subject=%s", subject, exc_info=True
                )

        asyncio.create_task(_pub())

    def _ensure_started(self) -> None:
        if not self._started or self._pipeline is None:
            raise RuntimeError("PipelineService is not started — call await svc.start() first")


__all__ = ["PipelineService"]
