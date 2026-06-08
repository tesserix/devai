"""Fiber-style pipeline runtime for DevAI.

Replaces the hardcoded LangGraph orchestrator with a composable model:

    Blueprint (YAML DAG)  →  Stages (PipelineStage instances)  →  Task (state machine)

Public surface:
    DevAITask, TaskState, StageResult, StageEvent — plain data
    PipelineStage, StageDeps, StageFactory       — the stage ABI
    Pipeline                                      — the orchestrator (lazy, see __getattr__)

Pipeline is exposed via __getattr__ so importing `devai.pipeline` doesn't
eagerly pull in `devai.blueprint`. This avoids a circular import: the
blueprint package depends on `devai.pipeline.types`, and importing
`devai.pipeline.Pipeline` at package-init time would force blueprint to
load before pipeline.types is fully initialised.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from devai.pipeline.interfaces import PipelineStage, StageDeps, StageFactory
from devai.pipeline.pipeline import Pipeline, PipelineError
from devai.pipeline.types import (
    DevAITask,
    StageEvent,
    StageEventPhase,
    StageResult,
    TaskState,
)


def __getattr__(name: str) -> Any:
    """PEP-562 lazy attribute access for Pipeline / PipelineError / PipelineService."""
    if name in {"Pipeline", "PipelineError"}:
        from devai.pipeline.pipeline import Pipeline, PipelineError

        return {"Pipeline": Pipeline, "PipelineError": PipelineError}[name]
    if name == "PipelineService":
        from devai.pipeline.service import PipelineService

        return PipelineService
    raise AttributeError(f"module 'devai.pipeline' has no attribute {name!r}")


__all__ = [
    "DevAITask",
    "Pipeline",
    "PipelineError",
    "PipelineService",
    "PipelineStage",
    "StageDeps",
    "StageEvent",
    "StageEventPhase",
    "StageFactory",
    "StageResult",
    "TaskState",
]
