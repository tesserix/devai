"""The one generic blueprint workflow.

``BlueprintWorkflow`` interprets *any* blueprint DAG durably. It reuses the exact
same ordering (``devai.blueprint.planner``) and condition DSL
(``devai.blueprint.conditions``) as the in-process executor, so a blueprint runs
identically on either backend — simple or complex.

Determinism: the workflow does only pure work — ordering, condition gating,
resumption-skip and result merging. Every side effect (the stage's real work, with
LangGraph/AgentRunner reasoning inside it) happens in the ``run_stage`` activity.
Concurrency within a topological level is expressed with ``asyncio.gather`` over
activity calls, which Temporal records and replays deterministically.

Level semantics mirror the executor: a level whose every runnable stage is marked
``parallel`` is fanned out concurrently; otherwise stages run sequentially so each
sees the previous one's handover.
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
    from devai.pipeline.types import TaskState

_DEFAULT_STAGE_TIMEOUT = 900
_DEFAULT_MAX_ATTEMPTS = 3


@workflow.defn(name="BlueprintWorkflow")
class BlueprintWorkflow:
    """Runs an arbitrary blueprint DAG as a durable workflow."""

    @workflow.run
    async def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        blueprint = blueprint_from_dict(payload["blueprint"])
        task = task_from_dict(payload["task"])
        self._default_timeout = int(
            payload.get("default_stage_timeout", _DEFAULT_STAGE_TIMEOUT)
        )
        self._max_attempts = int(payload.get("max_stage_attempts", _DEFAULT_MAX_ATTEMPTS))

        levels = topological_levels(blueprint.stages)
        workflow.logger.info(
            "BlueprintWorkflow %s: %d stages, %d levels",
            blueprint.name,
            len(blueprint.stages),
            len(levels),
        )

        for level in levels:
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

        # Mirror BlueprintExecutor.execute: promote a non-failed, non-terminal
        # task to COMPLETED. Direct assignment (no .transition) keeps the
        # workflow deterministic — no wall-clock reads.
        if not task.is_failed and not task.is_terminal:
            task.state = TaskState.COMPLETED

        return task_to_dict(task)

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
        """Merge one stage outcome into the task (declared-order, deterministic)."""
        if isinstance(res, BaseException):
            if should_continue_on_failure(spec.on_failure):
                workflow.logger.warning(
                    "stage %s failed, on_failure=continue: %s", spec.name, res
                )
                task.stages_failed.append(spec.name)
                return
            task.state = TaskState.STAGE_FAILED
            task.error = f"stage {spec.name!r} failed: {res}"
            task.failed_stage = spec.name
            task.stages_failed.append(spec.name)
            return

        result = stage_result_from_dict(res)
        if result.data:
            task.agent_context.update(result.data)
        if result.next_state is not None:
            task.state = result.next_state
        task.stages_completed.append(spec.name)
