"""No-op workflow adapter — graceful-degrade fallback and test double.

Returns the task untouched. Used when ``DEVAI_WORKFLOW_PROVIDER=noop`` or when a
real backend cannot be constructed. It satisfies "degrade, never crash": the pod
keeps serving even if orchestration is disabled.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .base import WorkflowAdapter

if TYPE_CHECKING:
    from devai.blueprint.loader import Blueprint
    from devai.pipeline.types import DevAITask

logger = logging.getLogger(__name__)


class NoopWorkflowAdapter(WorkflowAdapter):
    """Does nothing; returns the task unchanged."""

    async def run_blueprint(self, blueprint: Blueprint, task: DevAITask) -> DevAITask:
        logger.info(
            "Noop workflow adapter: blueprint %r not executed for task %s",
            blueprint.name,
            task.id,
        )
        return task

    @property
    def provider_name(self) -> str:
        return "noop"
