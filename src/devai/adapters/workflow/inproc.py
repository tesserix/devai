"""In-process workflow adapter — runs the blueprint directly in this process.

This is the default and keeps local dev + unit tests free of any Temporal server.
It is a thin wrapper over :class:`~devai.blueprint.executor.BlueprintExecutor`, so
behaviour is byte-for-byte identical to the pre-adapter pipeline.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .base import WorkflowAdapter

if TYPE_CHECKING:
    from devai.blueprint.executor import BlueprintExecutor
    from devai.blueprint.loader import Blueprint
    from devai.pipeline.types import DevAITask


class InProcWorkflowAdapter(WorkflowAdapter):
    """Executes blueprints inline via a :class:`BlueprintExecutor`."""

    def __init__(self, executor: BlueprintExecutor) -> None:
        self._executor = executor

    async def run_blueprint(self, blueprint: Blueprint, task: DevAITask) -> DevAITask:
        return await self._executor.execute(blueprint, task)

    @property
    def provider_name(self) -> str:
        return "inproc"
