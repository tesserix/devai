"""Worker-global execution context for stage activities.

A Temporal activity cannot carry live objects (SCM clients, DB pools) in its
payload — those must be reconstructed worker-side. The worker builds the
``StageDeps`` + ``StageRegistry`` once at startup and stashes them here; the
generic ``run_stage`` activity reads them. This is what lets one activity run
*any* stage of *any* blueprint without per-stage wiring.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from devai.blueprint.registry import StageRegistry
    from devai.pipeline.interfaces import StageDeps


@dataclass(slots=True)
class WorkerContext:
    registry: StageRegistry
    deps: StageDeps


_CONTEXT: WorkerContext | None = None


def set_worker_context(ctx: WorkerContext) -> None:
    global _CONTEXT
    _CONTEXT = ctx


def get_worker_context() -> WorkerContext:
    if _CONTEXT is None:
        raise RuntimeError(
            "worker context not initialised — call set_worker_context() in the "
            "Temporal worker before running activities"
        )
    return _CONTEXT
