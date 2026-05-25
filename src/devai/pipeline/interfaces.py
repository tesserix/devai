"""Stage ABI and dependency injection bundle.

Every stage in the registry implements `PipelineStage`. The blueprint
executor doesn't know or care what a stage actually does — it only sees
`name()`, `execute()`, `rollback()`, and the StageResult that comes back.

StageDeps is the bundle of shared services every stage needs (SCM client,
state manager, config, A2A bus, …). It's passed to stage factories at
registration time and held by closure; stages never reach into globals.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Protocol

from devai.pipeline.types import DevAITask, StageResult

if TYPE_CHECKING:
    from devai.adapters.memory.base import MemoryAdapter
    from devai.config import Settings
    from devai.core.event_bus import EventBus
    from devai.core.state import StateManager
    from devai.graph.a2a import A2ABus
    from devai.scm.base import SCMClient


@dataclass(frozen=True, slots=True)
class StageDeps:
    """Services that stage factories close over.

    Frozen — these are wired once at startup and don't change per task.
    Per-task state lives on the DevAITask itself.

    Any of these can be None for stages that don't need them (e.g. a
    pure analysis stage doesn't need the SCM client). Stages MUST tolerate
    None gracefully so unit tests can build a minimal StageDeps without
    standing up Redis / Postgres / GitHub.

    Adapter fields (memory, future: vector_store, event_bus, secrets, ...)
    let stages talk to integrations through stable interfaces; the
    factory layer picks the concrete backend via env-var config.
    """

    config: "Settings"
    scm: "SCMClient | None" = None
    state_manager: "StateManager | None" = None
    event_bus: "EventBus | None" = None
    a2a_bus: "A2ABus | None" = None

    # ── Adapters ─────────────────────────────────────────────────────
    # Constructed via devai.adapters.* factories. `None` means the
    # corresponding integration is disabled; stages must tolerate it.
    memory: "MemoryAdapter | None" = None

    # Pluggable LLM providers — None means "stages use their hardcoded
    # default provider" (the way existing agents do today). Once we move
    # to specializations-as-YAML, the spec declares its provider and we
    # plumb it through here.
    extra: dict[str, Any] | None = None


class PipelineStage(ABC):
    """The contract every stage implements.

    The blueprint executor calls `execute()` and consumes the StageResult.
    On failure, depending on the blueprint's `on_failure` policy, it may
    call `rollback()` to let the stage clean up any partial state it created.
    """

    @abstractmethod
    def name(self) -> str:
        """Stable identifier — matches the `stage:` key in the YAML."""

    @abstractmethod
    async def execute(self, task: DevAITask) -> StageResult:
        """Run the stage. Must be idempotent on retry.

        Implementations should:
        - Read context they need from `task.agent_context`.
        - Write outputs to StageResult.data (NOT directly to task.agent_context).
          The executor merges StageResult.data → task.agent_context after
          a successful return.
        - Raise to signal a hard failure; the executor maps the exception
          to a StageEvent(phase=FAILED) and applies the blueprint's
          on_failure policy.
        """

    async def rollback(self, task: DevAITask) -> None:
        """Compensating action when on_failure=rollback.

        Default is a no-op — most stages have nothing to undo. Stages that
        create external resources (issues, branches, sandbox pods) should
        override.
        """
        return None


# Stage factories are how stages get instantiated. The blueprint YAML
# declares `stage: <key>` and `config:` map; the registry looks up the
# factory by key and calls factory(deps, config) → PipelineStage.
StageFactory = Callable[[StageDeps, dict[str, str]], PipelineStage]


class StageRunner(Protocol):
    """Minimum interface the BlueprintExecutor requires from its host.

    Exists so unit tests can hand the executor a fake event sink without
    standing up the full Pipeline.
    """

    def emit_event(self, task: DevAITask, event: Any) -> None: ...
