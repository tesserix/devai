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

from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from devai.orchestration.context import get_worker_context
from devai.orchestration.serde import stage_result_to_dict, task_from_dict


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

    stage = ctx.registry.resolve(stage_key, ctx.deps, cfg)
    activity.logger.info("running stage %s (key=%s)", stage_name, stage_key)
    try:
        result = await stage.execute(task)
    except Exception as e:
        # A stage that may already have taken effect must not be replayed by
        # the RetryPolicy — surface it as non-retryable instead (ADR-0004).
        if getattr(e, "retry_unsafe", False):
            activity.logger.error("stage %s: outcome uncertain — refusing to replay", stage_name)
            raise ApplicationError(str(e), non_retryable=True) from e
        raise
    return stage_result_to_dict(result)
