"""``AgentStage`` — the one stage that runs an agent through the dispatcher.

Replaces the per-agent ``AgentAdapter`` subclasses (`stages/alm.py`) and, in a
later step, the bespoke ``run_specialization`` stage. Where ``AgentAdapter`` was
an abstract base each of 14 ALM stages subclassed (each overriding
`name`/`role_key`/`_make_agent`/`_next_state`/`_post_validate`), ``AgentStage``
is **data-configured**: a factory passes the agent, the handover key, the next
state, and an optional validator. Adding/altering a stage is a row, not a class.

It is behavior-identical to ``AgentAdapter`` by construction:

  - **stage-aware dispatch** — ensures ``task.current_stage`` is set so a
    stage-routing agent (e.g. ProductDirector → create_stories) sees the stage.
  - **per-principal + trial gate** — via the shared ``resolve_principal_run``
    (the exact preamble lifted out of ``AgentAdapter.execute``).
  - **typed result → StageResult** — via ``AgentResult.to_stage_result(task)``
    (the same nesting, scalar-surfacing, and structural-field mirroring).
  - **stub parity** — deps-unavailable returns a ``{output_key}_stub`` result
    with no validation, exactly as the adapter did.
  - **output validators** — the per-stage output contracts (require-PR, CI-truth,
    deploy-status) run after a successful result, raising to engage retry/heal.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from devai.agentruntime import AgentDispatcher, LegacyAgent
from devai.pipeline.interfaces import PipelineStage, StageDeps
from devai.pipeline.principal import resolve_principal_run
from devai.pipeline.types import DevAITask, StageResult, TaskState

if TYPE_CHECKING:
    from devai.agentruntime import Agent

logger = logging.getLogger(__name__)

# A stage output validator: inspect the agent's handover and raise to fail the
# stage (engaging the executor's retry/heal), or return to pass. Receives the
# stage name + handover key so one function can serve several stages.
Validator = Callable[..., Awaitable[None]]
# Builds the per-run instruction that overrides task.intent (e.g. a fix brief).
InstructionBuilder = Callable[[DevAITask], str]


class AgentStage(PipelineStage):
    """Run one agent (legacy or YAML) as a pipeline stage."""

    def __init__(
        self,
        deps: StageDeps,
        *,
        name: str,
        agent: Agent,
        output_key: str,
        next_state: TaskState | None = None,
        validator: Validator | None = None,
        instruction_builder: InstructionBuilder | None = None,
        extra_data: dict | None = None,
        trial_gate: bool = True,
    ) -> None:
        self.deps = deps
        self._name = name
        self._agent = agent
        self.output_key = output_key
        self._next_state = next_state
        self._validator = validator
        self._instruction_builder = instruction_builder
        self._extra_data = extra_data
        self._trial_gate = trial_gate
        self._dispatcher = AgentDispatcher(deps)

    def name(self) -> str:
        return self._name

    async def execute(self, task: DevAITask) -> StageResult:
        # Stage-aware dispatch parity: AgentAdapter passed `current_stage or
        # self.name()` into ALMState; the executor sets current_stage, but make
        # the fallback explicit so a stage-routing agent always sees a stage.
        task.current_stage = task.current_stage or self._name

        # Per-principal config/SCM + trial gate (may raise in strict mode) — the
        # exact preamble AgentAdapter ran, now shared.
        config, scm = await resolve_principal_run(self.deps, task, trial_gate=self._trial_gate, stage_name=self._name)

        instruction = self._instruction_builder(task) if self._instruction_builder else ""
        result = await self._dispatcher.dispatch(self._agent, task, instruction=instruction, config=config, scm=scm)

        # Stub parity: deps unavailable → a stub StageResult with no validation.
        if result.stub:
            return StageResult(
                next_state=self._next_state,
                message=f"{self._name} skipped — deps unavailable",
                data={f"{self.output_key}_stub": True},
            )

        # Force the stage's handover namespace; honor an agent-set next_state
        # (e.g. a high-risk SpecAgent parking for approval) else the stage's.
        result.output_key = self.output_key
        if result.next_state is None:
            result.next_state = self._next_state

        stage_result = result.to_stage_result(task)
        if self._extra_data:
            stage_result.data.update(self._extra_data)

        # Output contract: run AFTER the structural fields are mirrored onto the
        # task (validators read task.pr_number etc.). Raises → executor retries.
        if self._validator:
            await self._validator(self.deps, task, result.handover, stage_name=self._name, output_key=self.output_key)

        return stage_result


def legacy_agent_stage(
    deps: StageDeps,
    *,
    name: str,
    dotted: str,
    output_key: str,
    next_state: TaskState | None = None,
    validator: Validator | None = None,
    instruction_builder: InstructionBuilder | None = None,
    extra_data: dict | None = None,
) -> AgentStage:
    """Build an ``AgentStage`` that runs an existing ``BaseAgent`` by dotted path.

    The import is deferred to run time (via ``LegacyAgent.from_dotted``), so a
    missing optional dependency degrades to a stub at execute time — exactly the
    graceful-degrade the old ``_make_agent`` + ``_safe_agent`` provided.
    """
    agent = LegacyAgent.from_dotted(dotted, name=output_key, output_key=output_key)
    return AgentStage(
        deps,
        name=name,
        agent=agent,
        output_key=output_key,
        next_state=next_state,
        validator=validator,
        instruction_builder=instruction_builder,
        extra_data=extra_data,
        trial_gate=True,
    )


__all__ = ["AgentStage", "legacy_agent_stage"]
