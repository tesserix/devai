"""The Agent SDK contract — one shape every agent in DevAI satisfies.

Today DevAI has *three* disjoint "agent" abstractions and resolves them by
reflection in three different places with three different constructor
signatures (graph orchestrator, the run_specialization legacy bridge, and the
Job entrypoint). This module is the single contract that collapses them:

    class Agent(Protocol):
        name: str
        async def run(self, ctx: RunContext) -> AgentResult: ...

- ``RunContext`` is everything an agent needs to do its job — the task plus the
  per-principal-resolved capabilities (LLM, SCM, config overlay) and the shared
  ``StageDeps``. It is built **once** by the :class:`AgentDispatcher`, so an
  inline run and a Job run see byte-identical inputs.
- ``AgentResult`` is the typed return value that replaces the bare ``dict``
  patch agents hand back today. ``to_stage_result()`` bridges it back to the
  pipeline's ``StageResult`` without callers having to sniff dict keys.

Two concrete agents implement the protocol (see ``legacy.py`` and
``spec_agent.py``): ``LegacyAgent`` wraps an existing ``BaseAgent`` subclass;
``SpecAgent`` runs a YAML ``Specialization``. Both are dispatched the same way.

Nothing here imports an LLM SDK, the SCM layer, or the pipeline executor — it is
the plain contract everything else is built on, mirroring how
``adapters/<family>/base.py`` declares a family's minimum surface.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from devai.pipeline.types import DevAITask, StageResult, TaskState

if TYPE_CHECKING:
    from devai.adapters.llm.base import LLMAdapter
    from devai.agentruntime.dispatch import AgentDispatcher
    from devai.config import Settings
    from devai.pipeline.interfaces import StageDeps
    from devai.scm.base import SCMClient

logger = logging.getLogger(__name__)


# Well-known scalars an agent may emit that downstream stages, blueprint
# `condition:` expressions, and the dashboard read directly. They are surfaced
# flat (alongside the role-namespaced output bag) by ``to_stage_result`` — the
# union of the keys ``pipeline/stages/_base.py`` and ``stages/specialization.py``
# surface today, kept in one place so the contract has a single source of truth.
DEFAULT_SURFACE_KEYS: tuple[str, ...] = (
    "epic_issue_number",
    "pr_number",
    "branch_name",
    "review_decision",
    "security_decision",
    "deploy_status",
    "detected_tech_stack",
    "skill_profile_name",
    "build_status",
    "test_failed",
    "test_passed",
    "test_total",
    "stories",
    "technical_plan",
    "analyzed_requirements",
    # The boardroom's agreed approach. Surfaced so it can't be silently dropped
    # between the debate and the planner/implementer that must build "per the
    # agreement" (the engineering_manager + senior_developer read it).
    "boardroom_decision",
    # Scope signal from analysis — sizes decomposition and gates whether the
    # boardroom fires automatically (condition: output.scope_large).
    "scope_size",
    "scope_large",
    # Clarification — when analysis is too ambiguous, the run can pause for a
    # human (clarification gate) instead of building the wrong thing.
    "needs_clarification",
    "clarification_question",
    # Residual test-failure flag the no-CI test gate reads.
    "test_fix_applied",
)


@dataclass(slots=True)
class RunContext:
    """Everything an agent needs for one run.

    Built by :class:`devai.agentruntime.dispatch.AgentDispatcher` so the
    per-principal resolution (the triggering user's own LLM/SCM credentials and
    Settings overlay) happens exactly once, in one place, for every execution
    path. Agents read what they need and tolerate ``None`` — same discipline as
    ``StageDeps``.

    Fields:
        task          — the run state (handover bag, identity, structural fields)
        deps          — the shared, wired-once service bundle (memory, a2a, …)
        llm           — the LLM adapter resolved for ``task.triggered_by``
                        (their connector, falling back to the platform default)
        scm           — the SCM client resolved for ``task.triggered_by``
        config        — the Settings overlay for the principal (their keys/model),
                        falling back to ``deps.config``
        instruction   — overrides ``task.intent`` for this agent (a crew lead
                        uses this to hand a member a specific assignment)
        extra_context — merged on top of ``task.agent_context`` when the agent
                        builds its prompt (a crew lead passes a member its subtask)
        dispatcher    — back-reference to the dispatcher that built this context,
                        so an agent can recurse: ``await ctx.spawn(sub_agent)``
    """

    task: DevAITask
    deps: StageDeps
    llm: LLMAdapter | None = None
    scm: SCMClient | None = None
    config: Settings | None = None
    instruction: str = ""
    extra_context: dict[str, Any] = field(default_factory=dict)
    dispatcher: AgentDispatcher | None = None

    # ── Convenience accessors (read-only views onto the task) ──────────

    @property
    def agent_context(self) -> dict[str, Any]:
        """The handover bag prior stages wrote into."""
        return self.task.agent_context

    @property
    def triggered_by(self) -> str:
        return self.task.triggered_by or ""

    @property
    def trace_id(self) -> str:
        return self.task.trace_id or ""

    @property
    def repo(self) -> str:
        return self.task.repo

    @property
    def settings(self) -> Settings:
        """The effective settings for this run — the principal's overlay when
        resolved, else the platform config. Always non-None so callers can use
        it directly (mirrors ``StageDeps.settings_overlay``)."""
        return self.config or self.deps.config

    # ── Recursion primitive ────────────────────────────────────────────

    async def spawn(
        self,
        agent: Agent,
        *,
        instruction: str = "",
        extra_context: dict[str, Any] | None = None,
    ) -> AgentResult:
        """Run a sub-agent from inside this agent (recursive decomposition).

        This is the ROMA / RecursiveMAS primitive: a root agent decomposes its
        task and delegates a slice to a sub-agent, which may decompose further.
        The sub-agent runs on the same ``task`` (so it sees the handover bag and
        shares identity) but with its own ``instruction``/``extra_context``.

        Raises ``RuntimeError`` only if the context was built without a
        dispatcher (e.g. a hand-rolled test context) — normal dispatched runs
        always have one.
        """
        if self.dispatcher is None:
            raise RuntimeError(
                "RunContext.spawn: no dispatcher attached — this context was not "
                "created by AgentDispatcher, so sub-agents cannot be dispatched"
            )
        merged = {**self.extra_context, **(extra_context or {})}
        return await self.dispatcher.dispatch(agent, self.task, instruction=instruction, extra_context=merged)


@dataclass(slots=True)
class AgentResult:
    """The typed outcome of one agent run — replaces the bare ``dict`` patch.

    ``handover`` is the agent's output as a flat dict (what used to be the raw
    patch). ``output_key`` is the role-namespaced key the handover is *also*
    nested under in the handover bag (``requirements_analyst_output``, …),
    preserving the existing read convention. ``to_stage_result`` performs that
    nesting plus the well-known-scalar surfacing, so the pipeline boundary stays
    in one place instead of being re-implemented per stage.
    """

    ok: bool = True
    handover: dict[str, Any] = field(default_factory=dict)
    output_key: str = ""
    message: str = ""
    next_state: TaskState | None = None

    # Telemetry (populated by SpecAgent from the tool-loop runner; legacy agents
    # leave these at zero — their accounting flows through the usage ledger).
    turns: int = 0
    tool_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    # A degraded run (no LLM wired, deps missing) sets ``stub=True`` and an empty
    # or sentinel handover. ``error`` carries a soft failure that didn't raise.
    stub: bool = False
    error: str = ""

    # The agent's final free-text turn (before handover extraction). YAML specs
    # surface it under ``<name>_text`` in the handover bag for display/debug.
    final_text: str = ""

    # A2A traffic the agent produced (legacy agents merge inbox+outbox into the
    # patch; surfaced back into the handover bag by ``to_stage_result``).
    a2a_messages: list[Any] = field(default_factory=list)

    def to_stage_result(
        self,
        task: DevAITask | None = None,
        *,
        surface_keys: tuple[str, ...] = DEFAULT_SURFACE_KEYS,
    ) -> StageResult:
        """Bridge to the pipeline's ``StageResult``.

        - Nests ``handover`` under ``output_key`` (or merges it flat when no key)
          so downstream stages read it the same way they do today.
        - Surfaces well-known scalars flat for ``condition:`` expressions and the
          dashboard.
        - Re-surfaces A2A traffic into the handover bag.
        - When ``task`` is supplied, mirrors structural scalars
          (``pr_number``/``branch_name``/…) onto it — the side effect
          ``AgentAdapter._build_result`` performs today.
        """
        data: dict[str, Any] = {}
        if self.output_key:
            data[self.output_key] = self.handover
        else:
            data.update(self.handover)

        for key in surface_keys:
            value = self.handover.get(key)
            if value is not None:
                data[key] = value

        if self.a2a_messages:
            data["a2a_messages"] = self.a2a_messages

        if task is not None:
            self._mirror_structural_fields(task)

        return StageResult(next_state=self.next_state, message=self.message, data=data)

    def _mirror_structural_fields(self, task: DevAITask) -> None:
        """Copy the handover's structural scalars onto the task object."""
        patch = self.handover
        if isinstance(patch.get("epic_issue_number"), int):
            task.epic_issue_number = patch["epic_issue_number"]
        if isinstance(patch.get("pr_number"), int):
            task.pr_number = patch["pr_number"]
        if isinstance(patch.get("branch_name"), str):
            task.branch_name = patch["branch_name"]
        if isinstance(patch.get("story_issue_numbers"), list):
            task.story_issue_numbers = list(patch["story_issue_numbers"])


@runtime_checkable
class Agent(Protocol):
    """The one contract every runnable agent satisfies.

    Implementations: ``LegacyAgent`` (wraps a ``BaseAgent``), ``SpecAgent``
    (runs a YAML ``Specialization``), and any future native agent. The
    dispatcher only ever calls ``run(ctx)`` — it never reflects on a concrete
    type, which is what lets inline/Job/recursive execution share one path.
    """

    name: str

    async def run(self, ctx: RunContext) -> AgentResult: ...


# ─── Shared mapping helpers (the single home for ALMState ↔ AgentResult) ──────
#
# These collapse the three near-identical `_build_state` / `_build_alm_state`
# and `_build_result` implementations (stages/_base.py, stages/specialization.py)
# into one place, so `LegacyAgent` and any stage produce identical state and
# read back results identically.


def build_alm_state(ctx: RunContext) -> dict[str, Any]:
    """Project a ``RunContext`` into the ``ALMState`` slice a ``BaseAgent`` reads.

    Surfaces the structural task fields, threads caller identity (so the agent's
    self-built ``A2ABus`` stamps the originating user + trace id), folds in the
    full handover bag, and re-injects any recovery guidance the executor's heal
    loop produced for this stage — the union of what ``AgentAdapter._build_state``
    and ``_RunSpecializationStage._build_alm_state`` do today.
    """
    task = ctx.task
    state: dict[str, Any] = {
        "run_id": task.id,
        "repo_full_name": task.repo,
        "requirements": ctx.instruction or task.intent,
        "stage": task.current_stage or "",
        "branch_name": task.branch_name,
        "pr_number": task.pr_number,
        "epic_issue_number": task.epic_issue_number,
        "story_issue_numbers": list(task.story_issue_numbers),
        "trigger_type": task.trigger_type,
        # Identity — base_agent.run reads these off the state to scope A2A +
        # tracing. Pulling them up here means every bridged agent gets correct
        # attribution without per-agent code.
        "principal": task.principal or {},
        "trigger_actor": task.triggered_by or "",
        "trace_id": task.trace_id or "",
        # Everything prior stages handed over (includes any a2a_messages list).
        **task.agent_context,
    }

    # Per-dispatch context wins over the persistent handover bag. ``extra_context``
    # is the documented way a caller hands THIS run a value without mutating the
    # shared bag — a crew lead passing a member its subtask (``spawn``), or the
    # multi-story implement stage passing ``active_story_index`` per story. Folded
    # after ``agent_context`` so it overrides, mirroring ``spawn``'s own merge.
    if ctx.extra_context:
        state.update(ctx.extra_context)

    # Recovery guidance: the executor's recovery agent diagnosed a prior failed
    # attempt of THIS stage and injected corrective instructions. Fold them into
    # the requirements so the agent actually changes behavior. The raw failure
    # text is attacker-influenceable (CI logs, tool output) so it's fenced as
    # untrusted data; the diagnosis/guidance are the recovery agent's own output.
    heal = task.agent_context.get(f"heal:{task.current_stage or ''}")
    if isinstance(heal, dict) and heal.get("guidance"):
        try:
            from devai.services.prompt_guard import wrap_untrusted

            fenced = wrap_untrusted(str(heal.get("error", "")), "failure output", limit=800)
        except Exception:  # noqa: BLE001 — guard is an enhancer, never a blocker
            fenced = str(heal.get("error", ""))[:800]
        state["requirements"] = (
            f"{state.get('requirements') or task.intent}\n\n"
            "## RECOVERY GUIDANCE — the previous attempt of this stage FAILED\n"
            f"{fenced}\n"
            f"Diagnosis: {heal.get('diagnosis', '')}\n"
            f"Corrective instructions (follow these): {heal['guidance']}"
        )
    return state


def alm_patch_to_result(
    patch: dict[str, Any],
    *,
    output_key: str = "",
    message: str = "",
) -> AgentResult:
    """Wrap a ``BaseAgent.run`` patch dict in a typed ``AgentResult``.

    Mirrors ``AgentAdapter._build_result``'s message selection and A2A
    surfacing, but leaves the role-namespacing + scalar surfacing to
    ``AgentResult.to_stage_result`` so there is exactly one place that does it.
    """
    if not isinstance(patch, dict):
        patch = {"result": patch}
    a2a = patch.get("a2a_messages")
    return AgentResult(
        ok=True,
        handover=patch,
        output_key=output_key,
        message=message or patch.get("summary") or patch.get("implementation_summary") or "",
        a2a_messages=a2a if isinstance(a2a, list) else [],
    )


__all__ = [
    "DEFAULT_SURFACE_KEYS",
    "Agent",
    "AgentResult",
    "RunContext",
    "alm_patch_to_result",
    "build_alm_state",
]
