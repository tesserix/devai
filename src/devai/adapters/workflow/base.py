"""Base classes for the workflow (durable orchestration) adapter family.

The workflow family abstracts *how* a blueprint is executed — directly in this
process, or durably on a Temporal cluster — behind one tiny surface:

    final_task = await adapter.run_blueprint(blueprint, task)

Crucially the abstraction is **blueprint- and agent-agnostic**. There is exactly
one entry point and it takes any :class:`~devai.blueprint.loader.Blueprint`. A new
blueprint (simple or complex) or a new agent/stage needs *no* change here: the
adapter never names a stage, it just runs the DAG. Swapping durability backends is
a single env var (``DEVAI_WORKFLOW_PROVIDER``); the rest of the code is identical.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from devai.blueprint.loader import Blueprint
    from devai.pipeline.types import DevAITask


class WorkflowAdapter(ABC):
    """Abstract base for all workflow/orchestration adapters."""

    @abstractmethod
    async def run_blueprint(self, blueprint: Blueprint, task: DevAITask) -> DevAITask:
        """Execute every stage of *blueprint* against *task* and return the task.

        Implementations must be safe to call concurrently for distinct tasks and
        must never raise for ordinary stage failures — a failed stage is reflected
        on the returned task's ``state``/``error``, not as an exception.
        """
        ...

    @property
    @abstractmethod
    def provider_name(self) -> str:
        ...

    async def health_check(self) -> bool:
        return True


class WorkflowAdapterError(Exception):
    """Base error for the workflow adapter family."""


class AdapterNotInstalled(WorkflowAdapterError):
    """Raised when a backend SDK (e.g. ``temporalio``) is not installed."""


class AdapterNotConfigured(WorkflowAdapterError):
    """Raised when required configuration is missing."""
