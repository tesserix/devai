"""Blueprint executor — runs a Blueprint against a DevAITask.

Mirrors `internal/blueprint/executor.go`. The executor:

1. Topo-sorts the stages into levels (Kahn's algorithm).
2. Walks each level; stages within a level with `parallel: true` run via
   `asyncio.gather`, otherwise sequential.
3. Evaluates `condition:` per stage (skip if False).
4. Applies per-stage `timeout` via `asyncio.wait_for`.
5. Emits StageEvent(started|completed|failed|skipped) for every transition.
6. Handles `on_failure: stop | rollback | continue`.
7. Honors `shouldRunStageFromState` for resumption — already-completed
   stages on `task.stages_completed` are skipped on replay.

The executor is intentionally state-machine-agnostic. It records what
happened; the Pipeline class on top decides what state to transition the
task into based on the StageResult.next_state hint.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Iterable
from typing import Any

from devai.blueprint.conditions import evaluate as eval_condition
from devai.blueprint.loader import Blueprint, StageSpec
from devai.blueprint.planner import topological_levels
from devai.blueprint.registry import StageRegistry, StageRegistryError
from devai.pipeline.interfaces import PipelineStage, StageDeps
from devai.pipeline.types import (
    DevAITask,
    StageEvent,
    StageEventPhase,
    StageResult,
    TaskState,
)

logger = logging.getLogger(__name__)


class BlueprintExecutionError(Exception):
    """Raised when a blueprint can't be executed (cycle, missing stage, ...)."""


# Hook for streaming events to subscribers (SSE, audit log, JetStream
# bridge). Called synchronously from the executor loop with the
# in-progress task and the freshly-emitted event.
EventCallback = Callable[[DevAITask, StageEvent], None]


class BlueprintExecutor:
    """Runs a Blueprint against a DevAITask.

    Stateless across runs — instantiate once per Pipeline and reuse.
    `execute()` is async and yields control between stages so the
    Pipeline can run multiple tasks concurrently.
    """

    def __init__(
        self,
        registry: StageRegistry,
        deps: StageDeps,
        *,
        event_callback: EventCallback | None = None,
        default_stage_timeout: float = 900.0,
    ) -> None:
        self._registry = registry
        self._deps = deps
        self._event_cb = event_callback
        self._default_timeout = default_stage_timeout
        # Global transient-retry default; per-stage `retries:` overrides.
        self._default_retries = max(0, int(getattr(deps.config, "pipeline_stage_retries", 1) or 0))
        # Autonomy default for gate stages: "full" self-approves gates so
        # runs flow end-to-end; "gated" pauses for a human decision.
        self._default_autonomy = str(getattr(deps.config, "pipeline_default_autonomy", "full") or "full").lower()

    async def execute(self, blueprint: Blueprint, task: DevAITask) -> DevAITask:
        """Execute every stage in topo order. Returns the (mutated) task.

        Failure semantics:
            on_failure=stop      — short-circuit; task → STAGE_FAILED.
            on_failure=rollback  — invoke stage.rollback(), then stop.
            on_failure=continue  — record failure, keep going.
        """
        levels = _topo_sort(blueprint.stages)
        logger.info(
            "executing blueprint %s on task %s — %d stages in %d levels",
            blueprint.name,
            task.id,
            len(blueprint.stages),
            len(levels),
        )

        # Mark the run ACTIVE the moment execution begins. Without this the task
        # sits at QUEUED for the entire blueprint: the app-scaffold run_as_job
        # stages (scan/install_deps/seed_mocks) carry no `next_state`, so nothing
        # moves it off QUEUED even as stages clearly run. The dashboard then reads
        # state=queued, concludes the run hasn't started, and shows the QUEUED
        # badge + "Waiting for the agent…" with empty Logs/Events/Timeline even
        # though stage events are streaming. Per-stage sub-states (PLANNING/
        # IMPLEMENTING) still refine this; we only guarantee it leaves QUEUED.
        if task.state in (TaskState.PENDING, TaskState.QUEUED):
            task.transition(TaskState.RUNNING)
            if task.started_at is None:
                task.started_at = time.time()

        for level_idx, level in enumerate(levels):
            # Honor user run-control (pause / stop) at each stage boundary.
            # Returns False when the run was stopped (task already CANCELLED).
            if not await self._check_run_control(task):
                logger.info("blueprint %s stopped by user at level %d", blueprint.name, level_idx)
                break

            # Group stages within a level: parallel-marked stages all run
            # together, sequential stages run one at a time. In practice we
            # keep this simple — if every stage in a level has parallel=True
            # we gather them; otherwise we run sequentially within the level.
            parallel_in_level = [s for s in level if s.parallel]
            sequential_in_level = [s for s in level if not s.parallel]

            if parallel_in_level and not sequential_in_level:
                # All-parallel level
                await asyncio.gather(*(self._run_one(spec, task) for spec in parallel_in_level))
            else:
                # Mixed or all-sequential
                for spec in level:
                    await self._run_one(spec, task)

            if task.is_failed:
                logger.warning("blueprint %s halted at level %d due to failure", blueprint.name, level_idx)
                break

        if not task.is_failed and not task.is_terminal:
            task.transition(TaskState.COMPLETED)
        return task

    # ──────────────────────────────────────────────────────────────────
    # Internal: run control (pause / stop)
    # ──────────────────────────────────────────────────────────────────

    _CONTROL_POLL_SECONDS = 2.0
    _CONTROL_MAX_PAUSE_SECONDS = 3600.0

    async def _check_run_control(self, task: DevAITask) -> bool:
        """Poll the durable run-control flag at a stage boundary.

        Returns False if the run was stopped (and transitions the task to
        CANCELLED); blocks while paused (up to a max), then resumes. A no-op
        returning True when no StateManager exposes the control surface, so
        tests and minimal deps are unaffected. The Temporal workflow uses
        Signals instead — this drives the in-process path only.
        """
        sm = self._deps.state_manager
        getter = getattr(sm, "get_pipeline_control", None)
        if sm is None or getter is None:
            return True
        waited = 0.0
        while True:
            try:
                ctrl = await getter(task.id)
            except Exception:  # noqa: BLE001 — control is best-effort, never fatal
                return True
            if ctrl == "stopped":
                task.error = "stopped by user"
                task.failed_stage = task.current_stage or ""
                task.transition(TaskState.CANCELLED)
                return False
            if ctrl == "paused" and waited < self._CONTROL_MAX_PAUSE_SECONDS:
                await asyncio.sleep(self._CONTROL_POLL_SECONDS)
                waited += self._CONTROL_POLL_SECONDS
                continue
            return True

    # ──────────────────────────────────────────────────────────────────
    # Internal: stage execution
    # ──────────────────────────────────────────────────────────────────

    async def _run_one(self, spec: StageSpec, task: DevAITask) -> None:
        """Execute one stage spec, handling all gates (skip / condition /
        already-done / timeout / on_failure)."""

        # One factory for every event this stage emits so each carries the
        # agent persona + lane — the dashboard lights up the matching agent
        # card / phase group straight from the event, no blueprint re-read.
        def _ev(phase: StageEventPhase, **kw: Any) -> StageEvent:
            return StageEvent(
                spec.name,
                phase,
                stage_type=spec.type,
                gate=spec.gate,
                agent=spec.resolved_agent(),
                lane=spec.lane,
                **kw,
            )

        # Resumption: already done on a previous run.
        if spec.name in task.stages_completed:
            logger.debug("stage %s already completed — skipping", spec.name)
            self._emit(task, _ev(StageEventPhase.SKIPPED, message="already completed"))
            return

        # Condition gate.
        try:
            should_run = eval_condition(spec.condition, task)
        except ValueError as e:
            logger.error("stage %s: bad condition %r: %s", spec.name, spec.condition, e)
            task.stages_failed.append(spec.name)
            task.error = f"bad condition on {spec.name}: {e}"
            task.failed_stage = spec.name
            task.transition(TaskState.STAGE_FAILED)
            self._emit(task, _ev(StageEventPhase.FAILED, error=str(e)))
            return

        if not should_run:
            logger.info("stage %s: condition %r evaluated False — skipping", spec.name, spec.condition)
            task.stages_skipped.append(spec.name)
            self._emit(task, _ev(StageEventPhase.SKIPPED, message=f"condition: {spec.condition}"))
            return

        # Resolve the stage instance. We stamp `__stage_name` into the
        # config so generic stage handlers (`run_as_job`, `noop`, etc.)
        # can recover the YAML stage name without parsing the blueprint.
        try:
            cfg_with_name = {**spec.config, "__stage_name": spec.name}
            stage = self._registry.resolve(spec.stage, self._deps, cfg_with_name)
        except StageRegistryError as e:
            task.stages_failed.append(spec.name)
            task.error = str(e)
            task.failed_stage = spec.name
            task.transition(TaskState.STAGE_FAILED)
            self._emit(task, _ev(StageEventPhase.FAILED, error=str(e)))
            return

        # Human approval gate. `gate: true` stages WAIT for a decision when
        # the run's autonomy mode asks for it; in full-autonomy mode (the
        # default) the gate self-approves with an audit trail and the run
        # flows end-to-end without prompting.
        if spec.gate and not await self._resolve_gate(spec, task, _ev):
            return  # rejected/stopped at the gate — task already transitioned

        # Run it.
        task.current_stage = spec.name
        start = time.monotonic()
        self._emit(task, _ev(StageEventPhase.STARTED, message=spec.stage))

        timeout = spec.timeout_seconds or self._default_timeout
        # Transient-failure resilience: exceptions/timeouts re-run the stage
        # (with linear backoff) before on_failure semantics apply. Stages are
        # resume-idempotent by design — a retry is no riskier than the
        # pod-restart resume path that already re-runs unfinished stages.
        max_attempts = 1 + (spec.retries if spec.retries > 0 else self._default_retries)
        result: StageResult | object | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                result = await asyncio.wait_for(stage.execute(task), timeout=timeout)
                break
            except TimeoutError:
                duration_ms = (time.monotonic() - start) * 1000.0
                err = f"timed out after {timeout}s"
                logger.error("stage %s: %s (attempt %d/%d)", spec.name, err, attempt, max_attempts)
                if attempt >= max_attempts:
                    await self._handle_failure(spec, stage, task, err, TaskState.AGENT_TIMEOUT, duration_ms)
                    return
            except Exception as e:  # noqa: BLE001 — we want to catch all stage errors
                duration_ms = (time.monotonic() - start) * 1000.0
                logger.exception("stage %s raised (attempt %d/%d)", spec.name, attempt, max_attempts)
                if attempt >= max_attempts:
                    await self._handle_failure(spec, stage, task, str(e), TaskState.STAGE_FAILED, duration_ms)
                    return
            self._emit(
                task,
                _ev(
                    StageEventPhase.STARTED,
                    message=f"retrying after failure (attempt {attempt + 1}/{max_attempts})",
                ),
            )
            await asyncio.sleep(min(5.0 * attempt, 30.0))

        duration_ms = (time.monotonic() - start) * 1000.0

        # Success path — merge handover and advance state.
        if not isinstance(result, StageResult):
            err = f"stage {spec.name} returned {type(result).__name__}, expected StageResult"
            await self._handle_failure(spec, stage, task, err, TaskState.STAGE_FAILED, duration_ms)
            return

        task.merge_handover(result.data)
        if result.next_state is not None:
            task.transition(result.next_state)
        task.stages_completed.append(spec.name)
        task.current_stage = ""

        self._emit(task, _ev(StageEventPhase.COMPLETED, duration_ms=duration_ms, message=result.message))

    _GATE_POLL_SECONDS = 3.0
    _GATE_MAX_WAIT_SECONDS = 24 * 3600.0

    async def _resolve_gate(self, spec: StageSpec, task: DevAITask, _ev: Any) -> bool:
        """Resolve a human-approval gate before the stage runs.

        Returns True to proceed, False when the run stops here (rejected or
        stopped by run-control). Decision key:
        ``devai:pipeline:gate:{task_id}:{stage}`` — the same key the
        dashboard's Approve/Reject endpoints write.

        Autonomy (per-run ``agent_context['autonomy']``, falling back to
        ``DEVAI_PIPELINE_DEFAULT_AUTONOMY``):
          full   — self-approve with an audit decision; never prompt.
          gated  — transition to AWAITING_APPROVAL and poll for the human
                   decision (the dashboard banner is driven by this state).
        No Redis (tests/minimal deps) → proceed.
        """
        sm = self._deps.state_manager
        redis = getattr(sm, "redis", None) if sm is not None else None
        if redis is None:
            return True
        key = f"devai:pipeline:gate:{task.id}:{spec.name}"

        try:
            decision = await redis.get(key)
        except Exception:  # noqa: BLE001 — a Redis blip must not kill the run
            logger.exception("gate %s: decision read failed — proceeding", spec.name)
            return True
        autonomy = str(task.agent_context.get("autonomy") or self._default_autonomy).lower()

        if decision is None and autonomy != "gated":
            try:
                await redis.set(key, "approved", ex=86400)
                await redis.set(f"{key}:approver", "autonomy:full", ex=86400)
            except Exception:  # noqa: BLE001
                logger.exception("gate %s: auto-approve write failed — proceeding", spec.name)
            self._emit(task, _ev(StageEventPhase.STARTED, message="gate auto-approved (autonomy=full)"))
            return True

        if decision is None:
            # Pause for the human. The state flip is what makes the dashboard
            # banner appear (list_gates: reached + undecided = pending).
            prior_state = task.state
            task.current_stage = spec.name
            task.transition(TaskState.AWAITING_APPROVAL)
            self._emit(task, _ev(StageEventPhase.STARTED, message="waiting for human approval"))
            waited = 0.0
            while decision is None and waited < self._GATE_MAX_WAIT_SECONDS:
                if not await self._check_run_control(task):
                    return False  # stopped by user while waiting
                await asyncio.sleep(self._GATE_POLL_SECONDS)
                waited += self._GATE_POLL_SECONDS
                try:
                    decision = await redis.get(key)
                except Exception:  # noqa: BLE001
                    logger.exception("gate %s: decision poll failed", spec.name)
            if decision is None:
                task.error = f"approval gate {spec.name!r} timed out"
                task.failed_stage = spec.name
                task.transition(TaskState.CANCELLED)
                self._emit(task, _ev(StageEventPhase.FAILED, error=task.error))
                return False
            task.transition(prior_state if prior_state not in (TaskState.PENDING, TaskState.QUEUED) else TaskState.RUNNING)

        if str(decision).lower() == "rejected":
            task.error = f"rejected at gate {spec.name!r}"
            task.failed_stage = spec.name
            task.transition(TaskState.CANCELLED)
            self._emit(task, _ev(StageEventPhase.FAILED, error=task.error))
            return False
        return True

    async def _handle_failure(
        self,
        spec: StageSpec,
        stage: PipelineStage,
        task: DevAITask,
        error: str,
        failure_state: TaskState,
        duration_ms: float,
    ) -> None:
        task.stages_failed.append(spec.name)
        self._emit(
            task,
            StageEvent(
                spec.name,
                StageEventPhase.FAILED,
                duration_ms=duration_ms,
                error=error,
                stage_type=spec.type,
                gate=spec.gate,
                agent=spec.resolved_agent(),
                lane=spec.lane,
            ),
        )

        if spec.on_failure == "rollback":
            try:
                await stage.rollback(task)
            except Exception:  # noqa: BLE001 — rollback failure shouldn't mask the original
                logger.exception("rollback failed for stage %s", spec.name)

        if spec.on_failure in {"stop", "rollback"}:
            task.error = error
            task.failed_stage = spec.name
            task.transition(failure_state)
        # on_failure=continue: leave state alone, executor proceeds.
        task.current_stage = ""

    def _emit(self, task: DevAITask, event: StageEvent) -> None:
        task.record_event(event)
        if self._event_cb is not None:
            try:
                self._event_cb(task, event)
            except Exception:  # noqa: BLE001
                logger.exception("event callback failed for %s", event.stage)


# ──────────────────────────────────────────────────────────────────────────
# Topo sort: stages → levels
# ──────────────────────────────────────────────────────────────────────────


def _topo_sort(stages: Iterable[StageSpec]) -> list[list[StageSpec]]:
    """Kahn's algorithm → levels of stages with no inter-level dependencies.

    Delegates to :func:`devai.blueprint.planner.topological_levels` so the
    in-process executor and the Temporal workflow share one ordering routine.
    Re-raises cycle errors as :class:`BlueprintExecutionError`.
    """
    try:
        return topological_levels(list(stages))
    except ValueError as e:
        raise BlueprintExecutionError(str(e)) from e


__all__ = ["BlueprintExecutor", "BlueprintExecutionError"]
