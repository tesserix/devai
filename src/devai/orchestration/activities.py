"""The one generic stage activity.

A single activity runs *any* blueprint stage. It resolves the stage from the
worker's registry, executes it against the reconstructed task and returns the
result as a dict. Because it is generic, no new activity is ever needed for a new
agent, stage or blueprint.

If the stage raises, the activity propagates the error so Temporal applies the
declared ``RetryPolicy``; the workflow decides stop-vs-continue on final failure.
The exception is a ``retry_unsafe`` failure — one whose side effects may already
have happened — which is re-raised as a non-retryable ApplicationError.

The activity persists live progress but does not author timeline events: only the
workflow holds the stage spec, so only it can attribute an event to an agent and
lane. Recording here too produced a second, agent-less copy of every event.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from devai.blueprint.registry import StageRegistryError
from devai.orchestration.context import get_worker_context
from devai.orchestration.serde import stage_result_to_dict, task_from_dict, task_to_dict
from devai.pipeline.types import TaskState

_HEARTBEAT_INTERVAL_SECONDS = 10.0


async def _persist_progress(task: Any, state_manager: Any) -> None:
    persist = getattr(state_manager, "persist_task", None)
    if persist is None:
        return
    task.updated_at = time.time()
    try:
        await persist(task_to_dict(task))
    except Exception:  # noqa: BLE001 — progress persistence must not fail the activity
        activity.logger.warning("stage progress persistence failed for %s", task.id, exc_info=True)


async def _heartbeat_until_complete(
    stage_name: str,
    complete: asyncio.Event,
    task: Any,
    state_manager: Any,
) -> None:
    while not complete.is_set():
        activity.heartbeat({"stage": stage_name})
        try:
            await asyncio.wait_for(complete.wait(), timeout=_HEARTBEAT_INTERVAL_SECONDS)
        except TimeoutError:
            await _persist_progress(task, state_manager)


@activity.defn(name="publish_progress")
async def publish_progress_activity(task_dict: dict[str, Any]) -> None:
    """Publish a workflow-side task snapshot to shared state.

    A workflow cannot do I/O, so terminal stage events — and the state ahead of a
    long approval gate — reach the dashboard only through this activity.
    Best-effort: ``persist_task`` is last-writer-wins on ``updated_at``, and a
    reporting failure must never fail the run.
    """
    state_manager = getattr(get_worker_context().deps, "state_manager", None)
    persist = getattr(state_manager, "persist_task", None)
    if persist is None:
        return
    try:
        await persist(task_dict)
    except Exception:  # noqa: BLE001 — reporting must not fail the run
        activity.logger.warning("progress snapshot failed for %s", task_dict.get("id"), exc_info=True)


@activity.defn(name="run_stage")
async def run_stage_activity(
    stage_key: str,
    stage_name: str,
    config: dict[str, str],
    task_dict: dict[str, Any],
) -> dict[str, Any]:
    ctx = get_worker_context()
    task = task_from_dict(task_dict)

    cfg = dict(config)
    cfg["__stage_name"] = stage_name  # parity with BlueprintExecutor

    try:
        stage = ctx.registry.resolve(stage_key, ctx.deps, cfg)
    except (StageRegistryError, TypeError, ValueError) as exc:
        raise ApplicationError(
            str(exc),
            type="StageConfigurationError",
            non_retryable=True,
        ) from exc

    activity.logger.info("running stage %s (key=%s)", stage_name, stage_key)
    if task.state in (TaskState.PENDING, TaskState.QUEUED):
        task.transition(TaskState.RUNNING)
        if task.started_at is None:
            task.started_at = time.time()
    task.current_stage = stage_name
    state_manager = getattr(ctx.deps, "state_manager", None)
    await _persist_progress(task, state_manager)

    complete = asyncio.Event()
    heartbeat = asyncio.create_task(_heartbeat_until_complete(stage_name, complete, task, state_manager))
    try:
        result = await stage.execute(task)
        task.merge_handover(result.data)
        if result.next_state is not None:
            task.transition(result.next_state)
        if stage_name not in task.stages_completed:
            task.stages_completed.append(stage_name)
        task.current_stage = ""
        return stage_result_to_dict(result)
    except Exception as e:
        # A stage that may already have taken effect must not be replayed by
        # the RetryPolicy — surface it as non-retryable instead (ADR-0004).
        if getattr(e, "retry_unsafe", False):
            activity.logger.error("stage %s: outcome uncertain — refusing to replay", stage_name)
            raise ApplicationError(str(e), non_retryable=True) from e
        raise
    finally:
        complete.set()
        await heartbeat
        await _persist_progress(task, state_manager)
