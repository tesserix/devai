"""The one generic blueprint workflow — durable, signal-controllable.

``BlueprintWorkflow`` interprets *any* blueprint DAG durably. It reuses the exact
same ordering (``devai.blueprint.planner``) and condition DSL
(``devai.blueprint.conditions``) as the in-process executor, so a blueprint runs
identically on either backend — simple or complex.

Determinism: the workflow does only pure work — ordering, condition gating,
resumption-skip and result merging. Every side effect (the stage's real work, with
LangGraph/AgentRunner reasoning inside it) happens in the ``run_stage`` activity.

Durable control (the Temporal-native answer to the in-process Redis flag):
  * **pause / resume / stop** — Signals flip workflow state; the loop blocks on
    ``workflow.wait_condition`` at each stage boundary. Pause can hold for days
    across worker restarts with no pod keeping state.
  * **approval gates** — a stage marked ``gate: true`` blocks on an approval
    Signal (``approve``/``reject`` carrying the gate name) before the pipeline
    proceeds. This is the durable human-in-the-loop.
  * **status** — a Query exposes paused/stopped/approvals for the dashboard.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from devai.blueprint.conditions import evaluate as eval_condition
    from devai.blueprint.planner import should_continue_on_failure, topological_levels
    from devai.orchestration.activities import run_stage_activity
    from devai.orchestration.serde import (
        blueprint_from_dict,
        stage_result_from_dict,
        task_from_dict,
        task_to_dict,
    )
    from devai.pipeline.types import FAILURE_STATES, TaskState

_DEFAULT_STAGE_TIMEOUT = 900
_DEFAULT_MAX_ATTEMPTS = 3


def _result_failed(result: Any) -> bool:
    """True when a (non-raised) StageResult signals a soft failure.

    A stage that catches its own error returns a StageResult rather than
    raising; without this check the workflow would record it as completed and
    ignore ``on_failure``. We treat three signals as failure, in priority
    order, so the contract works whether or not the StageResult dataclass
    grows a dedicated ``ok``/``failed`` field:
      1. an explicit ``failed: true`` in the handover data,
      2. an explicit ``ok: false`` in the handover data,
      3. ``next_state`` landing in a FAILURE_STATE.
    """
    data = getattr(result, "data", None) or {}
    if isinstance(data, dict):
        if data.get("failed") is True:
            return True
        if data.get("ok") is False:
            return True
    next_state = getattr(result, "next_state", None)
    return next_state is not None and next_state in FAILURE_STATES


@workflow.defn(name="BlueprintWorkflow")
class BlueprintWorkflow:
    """Runs an arbitrary blueprint DAG as a durable, signal-controllable workflow."""

    def __init__(self) -> None:
        self._paused = False
        self._stopped = False
        self._approvals: dict[str, str] = {}
        self._default_timeout = _DEFAULT_STAGE_TIMEOUT
        self._max_attempts = _DEFAULT_MAX_ATTEMPTS

    # ── Signals (durable control) ──────────────────────────────────────
    @workflow.signal
    def pause(self) -> None:
        self._paused = True

    @workflow.signal
    def resume(self) -> None:
        self._paused = False

    @workflow.signal
    def stop(self) -> None:
        self._stopped = True

    @workflow.signal
    def approve(self, gate: str) -> None:
        self._approvals[gate] = "approved"

    @workflow.signal
    def reject(self, gate: str) -> None:
        self._approvals[gate] = "rejected"

    @workflow.query
    def status(self) -> dict[str, Any]:
        return {
            "paused": self._paused,
            "stopped": self._stopped,
            "approvals": dict(self._approvals),
        }

    # ── Main loop ──────────────────────────────────────────────────────
    @workflow.run
    async def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        blueprint = blueprint_from_dict(payload["blueprint"])
        task = task_from_dict(payload["task"])
        self._default_timeout = int(payload.get("default_stage_timeout", _DEFAULT_STAGE_TIMEOUT))
        self._max_attempts = int(payload.get("max_stage_attempts", _DEFAULT_MAX_ATTEMPTS))

        levels = topological_levels(blueprint.stages)
        workflow.logger.info(
            "BlueprintWorkflow %s: %d stages, %d levels",
            blueprint.name,
            len(blueprint.stages),
            len(levels),
        )

        for level in levels:
            # Durable pause / stop at the stage boundary.
            await workflow.wait_condition(lambda: not self._paused or self._stopped)
            if self._stopped:
                self._cancel(task, "stopped by user")
                break

            runnable = []
            for spec in level:
                if spec.name in task.stages_completed:
                    continue  # resumption: already done
                if spec.condition and not eval_condition(spec.condition, task):
                    workflow.logger.info("stage %s skipped (condition false)", spec.name)
                    task.stages_skipped.append(spec.name)
                    continue
                runnable.append(spec)

            if not runnable:
                continue

            all_parallel = all(s.parallel for s in runnable)
            if all_parallel and len(runnable) > 1:
                snapshot = task_to_dict(task)
                results = await asyncio.gather(
                    *(self._run_stage(spec, snapshot) for spec in runnable),
                    return_exceptions=True,
                )
                for spec, res in zip(runnable, results, strict=True):
                    self._apply(task, spec, res)
            else:
                for spec in runnable:
                    res = await self._run_stage_safe(spec, task_to_dict(task))
                    self._apply(task, spec, res)

            if task.is_failed:
                workflow.logger.warning("blueprint %s halted at level", blueprint.name)
                break

            # Durable approval gates: a completed `gate: true` stage blocks the
            # pipeline on an approve/reject Signal before dependents run.
            for spec in runnable:
                if spec.gate and spec.name in task.stages_completed:
                    await self._await_gate(spec, task)
            if task.is_failed or task.is_terminal:
                break

        # Mirror BlueprintExecutor.execute: promote a non-failed, non-terminal
        # task to COMPLETED. Direct assignment keeps the workflow deterministic.
        if not task.is_failed and not task.is_terminal:
            task.state = TaskState.COMPLETED

        return task_to_dict(task)

    # ── Helpers ────────────────────────────────────────────────────────
    async def _await_gate(self, spec: Any, task: Any) -> None:
        """Block on a durable approval Signal for a gate stage."""
        gate = spec.name
        workflow.logger.info("gate %s: awaiting approval signal", gate)
        await workflow.wait_condition(lambda: gate in self._approvals or self._stopped)
        if self._stopped:
            self._cancel(task, "stopped by user")
            return
        if self._approvals.get(gate) == "rejected":
            self._cancel(task, f"gate {gate!r} rejected")

    def _cancel(self, task: Any, reason: str) -> None:
        task.state = TaskState.CANCELLED
        task.error = reason

    async def _run_stage(self, spec: Any, task_snapshot: dict[str, Any]) -> dict[str, Any]:
        timeout = spec.timeout_seconds or self._default_timeout
        return await workflow.execute_activity(
            run_stage_activity,
            args=[spec.stage, spec.name, dict(spec.config), task_snapshot],
            start_to_close_timeout=timedelta(seconds=timeout),
            retry_policy=RetryPolicy(maximum_attempts=self._max_attempts),
        )

    async def _run_stage_safe(self, spec: Any, task_snapshot: dict[str, Any]) -> Any:
        try:
            return await self._run_stage(spec, task_snapshot)
        except Exception as exc:  # noqa: BLE001 — surfaced to _apply for policy
            return exc

    def _apply(self, task: Any, spec: Any, res: Any) -> None:
        """Merge one stage outcome into the task (declared-order, deterministic).

        Two failure shapes are honored so ``on_failure`` is respected on either
        execution path:
          * a raised exception (hard failure — activity raised / timed out), and
          * a *soft* failure — the stage caught its own error and returned a
            StageResult that signals failure (an explicit ``ok=False`` /
            ``failed=True`` flag in its data, or ``next_state`` landing in a
            FAILURE_STATE). Previously a soft failure in a parallel/gate level
            was merged as a completed stage, so ``on_failure: stop`` never fired.
        """
        if isinstance(res, BaseException):
            self._record_failure(task, spec, f"stage {spec.name!r} failed: {res}")
            return

        result = stage_result_from_dict(res)

        # Soft failure: the stage returned (didn't raise) but reported failure.
        if _result_failed(result):
            reason = result.message or f"stage {spec.name!r} reported failure"
            # Still merge any handover the stage produced before it failed, so
            # downstream diagnostics / rollback have its partial output.
            if result.data:
                task.agent_context.update(result.data)
            self._record_failure(task, spec, reason, soft_state=result.next_state)
            return

        if result.data:
            task.agent_context.update(result.data)
        if result.next_state is not None:
            task.state = result.next_state
        task.stages_completed.append(spec.name)

    def _record_failure(self, task: Any, spec: Any, reason: str, *, soft_state: Any = None) -> None:
        """Apply a stage failure (hard or soft) honoring ``on_failure``."""
        task.stages_failed.append(spec.name)
        if should_continue_on_failure(spec.on_failure):
            workflow.logger.warning("stage %s failed, on_failure=continue: %s", spec.name, reason)
            return
        # stop | rollback: halt the run. Preserve the stage's own failure state
        # when it gave one (soft path), else the generic STAGE_FAILED.
        task.state = soft_state if soft_state in FAILURE_STATES else TaskState.STAGE_FAILED
        task.error = reason
        task.failed_stage = spec.name
