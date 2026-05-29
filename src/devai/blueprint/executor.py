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

        for level_idx, level in enumerate(levels):
            # Group stages within a level: parallel-marked stages all run
            # together, sequential stages run one at a time. In practice we
            # keep this simple — if every stage in a level has parallel=True
            # we gather them; otherwise we run sequentially within the level.
            parallel_in_level = [s for s in level if s.parallel]
            sequential_in_level = [s for s in level if not s.parallel]

            if parallel_in_level and not sequential_in_level:
                # All-parallel level
                await asyncio.gather(
                    *(self._run_one(spec, task) for spec in parallel_in_level)
                )
            else:
                # Mixed or all-sequential
                for spec in level:
                    await self._run_one(spec, task)

            if task.is_failed:
                logger.warning(
                    "blueprint %s halted at level %d due to failure", blueprint.name, level_idx
                )
                break

        if not task.is_failed and not task.is_terminal:
            task.transition(TaskState.COMPLETED)
        return task

    # ──────────────────────────────────────────────────────────────────
    # Internal: stage execution
    # ──────────────────────────────────────────────────────────────────

    async def _run_one(self, spec: StageSpec, task: DevAITask) -> None:
        """Execute one stage spec, handling all gates (skip / condition /
        already-done / timeout / on_failure)."""

        # Resumption: already done on a previous run.
        if spec.name in task.stages_completed:
            logger.debug("stage %s already completed — skipping", spec.name)
            self._emit(task, StageEvent(spec.name, StageEventPhase.SKIPPED, message="already completed"))
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
            self._emit(task, StageEvent(spec.name, StageEventPhase.FAILED, error=str(e)))
            return

        if not should_run:
            logger.info("stage %s: condition %r evaluated False — skipping", spec.name, spec.condition)
            task.stages_skipped.append(spec.name)
            self._emit(task, StageEvent(spec.name, StageEventPhase.SKIPPED, message=f"condition: {spec.condition}"))
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
            self._emit(task, StageEvent(spec.name, StageEventPhase.FAILED, error=str(e)))
            return

        # Run it.
        task.current_stage = spec.name
        start = time.monotonic()
        self._emit(
            task,
            StageEvent(
                spec.name,
                StageEventPhase.STARTED,
                message=spec.stage,
                stage_type=spec.type,
                gate=spec.gate,
            ),
        )

        timeout = spec.timeout_seconds or self._default_timeout
        try:
            result = await asyncio.wait_for(stage.execute(task), timeout=timeout)
        except asyncio.TimeoutError:
            duration_ms = (time.monotonic() - start) * 1000.0
            err = f"timed out after {timeout}s"
            logger.error("stage %s: %s", spec.name, err)
            await self._handle_failure(spec, stage, task, err, TaskState.AGENT_TIMEOUT, duration_ms)
            return
        except Exception as e:  # noqa: BLE001 — we want to catch all stage errors
            duration_ms = (time.monotonic() - start) * 1000.0
            logger.exception("stage %s raised", spec.name)
            await self._handle_failure(spec, stage, task, str(e), TaskState.STAGE_FAILED, duration_ms)
            return

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

        self._emit(
            task,
            StageEvent(
                spec.name,
                StageEventPhase.COMPLETED,
                duration_ms=duration_ms,
                message=result.message,
                stage_type=spec.type,
                gate=spec.gate,
            ),
        )

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

    Stages in level N may run in any order or in parallel as long as
    they don't depend on each other (they don't, by construction).

    Raises on cycles.
    """
    stages = list(stages)
    by_name = {s.name: s for s in stages}
    in_deg: dict[str, int] = {s.name: len(s.depends_on) for s in stages}
    levels: list[list[StageSpec]] = []
    remaining = set(by_name.keys())

    while remaining:
        ready = [name for name in remaining if in_deg[name] == 0]
        if not ready:
            raise BlueprintExecutionError(
                f"cycle in blueprint — stuck on stages: {sorted(remaining)}"
            )

        # Stable ordering — match YAML declaration order within the level.
        ready_sorted = sorted(ready, key=lambda n: [s.name for s in stages].index(n))
        levels.append([by_name[n] for n in ready_sorted])

        for name in ready:
            remaining.discard(name)

        # Decrement deps for the next round
        for name in remaining:
            spec = by_name[name]
            in_deg[name] = sum(1 for dep in spec.depends_on if dep in remaining)

    return levels


__all__ = ["BlueprintExecutor", "BlueprintExecutionError"]
