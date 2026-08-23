"""The one generic stage activity.

A single activity runs *any* blueprint stage. It resolves the stage from the
worker's registry, executes it against the reconstructed task and returns the
result as a dict. Because it is generic, no new activity is ever needed for a new
agent, stage or blueprint.

If the stage raises, the activity propagates the error so Temporal applies the
declared ``RetryPolicy``; the workflow decides stop-vs-continue on final failure.
The exception is a ``retry_unsafe`` failure — one whose side effects may already
have happened — which is re-raised as a non-retryable ApplicationError.
"""

from __future__ import annotations

import asyncio
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from devai.blueprint.registry import StageRegistryError
from devai.orchestration.context import get_worker_context
from devai.orchestration.serde import stage_result_to_dict, task_from_dict

_HEARTBEAT_INTERVAL_SECONDS = 10.0


async def _heartbeat_until_complete(stage_name: str, complete: asyncio.Event) -> None:
    while not complete.is_set():
        activity.heartbeat({"stage": stage_name})
        try:
            await asyncio.wait_for(complete.wait(), timeout=_HEARTBEAT_INTERVAL_SECONDS)
        except TimeoutError:
            continue


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
    complete = asyncio.Event()
    heartbeat = asyncio.create_task(_heartbeat_until_complete(stage_name, complete))
    try:
        result = await stage.execute(task)
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
