"""Factory — pick the workflow (durable orchestration) backend at runtime.

Reads ``settings.workflow_provider`` (env: ``DEVAI_WORKFLOW_PROVIDER``):

    inproc     → InProcWorkflowAdapter   (default; runs blueprints in this process)
    temporal   → TemporalWorkflowAdapter (durable; needs the temporalio SDK + cluster)
    noop       → NoopWorkflowAdapter     (disabled / degrade)

Graceful degradation (identical discipline to every other adapter family):
  - Unknown provider     → log + InProc
  - ``temporalio`` absent → log + InProc
  - Cluster unreachable  → handled at call time inside TemporalWorkflowAdapter,
                           which falls back to the InProc adapter per-run.

The factory needs the in-process :class:`BlueprintExecutor` because both the
``inproc`` provider and the ``temporal`` provider's degrade path run blueprints
locally through it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .inproc import InProcWorkflowAdapter
from .noop import NoopWorkflowAdapter

if TYPE_CHECKING:
    from devai.blueprint.executor import BlueprintExecutor
    from devai.config import Settings

    from .base import WorkflowAdapter

logger = logging.getLogger(__name__)

KNOWN_PROVIDERS = ("inproc", "temporal", "noop")


def create_workflow_adapter(
    settings: Settings,
    *,
    executor: BlueprintExecutor,
) -> WorkflowAdapter:
    """Resolve ``settings.workflow_provider`` to a WorkflowAdapter. Never raises."""
    provider = (getattr(settings, "workflow_provider", "inproc") or "inproc").lower()
    inproc = InProcWorkflowAdapter(executor)

    if provider in ("", "inproc"):
        return inproc
    if provider == "noop":
        logger.info("workflow_provider=noop — blueprint execution disabled")
        return NoopWorkflowAdapter()
    if provider == "temporal":
        try:
            import temporalio  # noqa: F401  (presence check only)
        except ImportError:
            logger.warning(
                "workflow_provider=temporal but 'temporalio' is not installed — "
                "using in-process executor. Install devai[temporal]."
            )
            return inproc
        from .temporal import TemporalWorkflowAdapter

        logger.info("workflow_provider=temporal — durable orchestration active")
        return TemporalWorkflowAdapter(settings, fallback=inproc)

    logger.warning(
        "workflow_provider=%r is unknown (known: %s) — using in-process executor",
        provider,
        ", ".join(KNOWN_PROVIDERS),
    )
    return inproc


__all__ = ["KNOWN_PROVIDERS", "create_workflow_adapter"]
