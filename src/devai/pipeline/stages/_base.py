"""Shared infrastructure for stage adapters.

The bulk of the existing DevAI code is in `devai.agents/*` — each agent is
a class with `__init__(scm, state_manager, config, event_bus)` and an
async `run(state: ALMState) -> dict`. The Fiber-style runtime has its own
task model (DevAITask + agent_context).

`AgentAdapter` bridges the two: it builds an ALMState slice from the
DevAITask, runs the agent, and translates the returned patch dict back
into StageResult.data keyed under a role namespace.

Subclasses declare:
    agent_factory   — zero-arg callable that returns the constructed agent
    role_key        — handover key the result is written under
    state_keys      — which DevAITask fields to surface into ALMState
    output_keys     — which ALMState patch keys to keep (None = all)
    next_state      — TaskState to transition to on success (optional)

This pattern is intentionally thin. As the migration progresses, agents
will be replaced by YAML specializations and this adapter goes away — but
for now it lets the YAML blueprint runtime drive existing agent classes
without rewriting them.
"""

from __future__ import annotations

import contextvars
import logging
from abc import abstractmethod
from collections.abc import Callable
from typing import Any

from devai.pipeline.interfaces import PipelineStage, StageDeps
from devai.pipeline.types import DevAITask, StageResult, TaskState

logger = logging.getLogger(__name__)

# The settings the CURRENT agent should construct its LLM from. AgentAdapter
# sets this to the triggering user's settings overlay for the duration of the
# agent run, so the legacy agents (which build their LLM from `config`) use the
# USER's provider/keys/model — not the global platform config. async-safe and
# per-task (each run executes in its own context), so concurrent runs never
# cross creds.
_agent_config_ctx: contextvars.ContextVar[Any] = contextvars.ContextVar("devai_agent_config", default=None)


def current_agent_config(default: Any) -> Any:
    """The per-run agent config override, or ``default`` (deps.config)."""
    return _agent_config_ctx.get(None) or default


def run_correlation_label(task_id: str) -> str:
    """Label tying every artifact (issues, epic, stories, bugs, PRs) back to
    the fleet run that produced it — e.g. `devai:run:c0ccd293f7`."""
    return f"devai:run:{(task_id or '').removeprefix('devai-')[:10]}"


class AgentAdapter(PipelineStage):
    """Bridge from DevAITask → existing agent.run(ALMState) → StageResult.

    Subclasses must override `name()` and `_make_agent()`. Most subclasses
    can rely on the default `_build_state()` / `_extract_outputs()` logic.
    """

    def __init__(self, deps: StageDeps, config: dict[str, str]) -> None:
        self.deps = deps
        self.config = config

    # ── PipelineStage surface ───────────────────────────────────────────

    async def execute(self, task: DevAITask) -> StageResult:
        # Construction includes the lazy `from devai.agents.X import Y` —
        # which itself can raise ImportError when an optional dep (ulid,
        # langgraph, …) is missing in a slim environment. Swallow both
        # here so a missing optional dep degrades to a stub rather than
        # halting the whole blueprint.
        # Resolve the triggering user's settings overlay so the agent we build
        # uses THEIR provider/keys/model (their connector), not the global
        # platform config. Set as a contextvar that _safe_agent reads during
        # construction. Falls back to deps.config when the user configured
        # nothing or resolution fails.
        agent_config = self.deps.config
        try:
            resolver = getattr(self.deps, "llm_resolver", None)
            if resolver is not None and getattr(task, "triggered_by", ""):
                overlay = await resolver.settings_for_email(task.triggered_by)
                if overlay is not None and overlay is not self.deps.config:
                    agent_config = overlay
        except Exception:  # noqa: BLE001
            logger.debug("stage %s: per-user config resolution failed", self.name(), exc_info=True)

        token = _agent_config_ctx.set(agent_config)
        try:
            agent = self._make_agent()
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "stage %s: _make_agent raised %s — returning stub StageResult",
                self.name(),
                e,
            )
            agent = None
        finally:
            _agent_config_ctx.reset(token)

        if agent is None:
            logger.warning(
                "stage %s: dependencies unavailable — returning stub StageResult",
                self.name(),
            )
            return StageResult(
                next_state=self._next_state_on_stub(),
                message=f"{self.name()} skipped — deps unavailable",
                data={f"{self.role_key()}_stub": True},
            )

        state = self._build_state(task)
        try:
            patch = await agent.run(state)
        except Exception:  # noqa: BLE001 — bubble up as a stage failure
            logger.exception("stage %s: agent failed", self.name())
            raise

        result = self._build_result(task, patch)
        # Output-contract validation: agents can finish their tool loop with
        # narrative text but no real side effects (a 51-minute implement run
        # produced NO branch/PR and still 'completed'). Subclasses raise here
        # so the executor's retry kicks in and persistent emptiness fails
        # VISIBLY instead of flowing downstream as success.
        await self._post_validate(task, patch)
        return result

    # ── Hooks subclasses override ───────────────────────────────────────

    @abstractmethod
    def name(self) -> str: ...

    def role_key(self) -> str:
        """Handover key prefix. Default: stage name."""
        return self.name()

    @abstractmethod
    def _make_agent(self) -> Any | None:
        """Construct the agent instance. Return None when deps missing."""

    def _next_state(self) -> TaskState | None:
        """TaskState to transition the task to on success."""
        return None

    def _next_state_on_stub(self) -> TaskState | None:
        """TaskState when deps missing and we're returning a stub."""
        return self._next_state()

    def _build_state(self, task: DevAITask) -> dict[str, Any]:
        """Construct the ALMState slice the agent expects.

        Default implementation surfaces enough of DevAITask that
        `analyze_requirements` / `create_epic` / etc. can run. Subclasses
        can override to pass additional context.
        """
        state = {
            "run_id": task.id,
            "repo_full_name": task.repo,
            "requirements": task.intent,
            "stage": task.current_stage or self.name(),
            "branch_name": task.branch_name,
            "pr_number": task.pr_number,
            "epic_issue_number": task.epic_issue_number,
            "story_issue_numbers": list(task.story_issue_numbers),
            "trigger_type": task.trigger_type,
            # Pass through anything previous stages wrote — agents read
            # their inputs from here under role-namespaced keys.
            **task.agent_context,
        }
        # Recovery guidance: the executor's recovery agent diagnosed a prior
        # failed attempt of THIS stage and injected corrective instructions —
        # fold them into the requirements so the agent actually changes
        # behavior instead of repeating the same failure.
        heal = task.agent_context.get(f"heal:{task.current_stage or self.name()}")
        if isinstance(heal, dict) and heal.get("guidance"):
            state["requirements"] = (
                f"{state.get('requirements') or task.intent}\n\n"
                "## RECOVERY GUIDANCE — the previous attempt of this stage FAILED\n"
                f"Failure: {heal.get('error', '')}\n"
                f"Diagnosis: {heal.get('diagnosis', '')}\n"
                f"Corrective instructions (follow these): {heal['guidance']}"
            )
        return state

    async def _post_validate(self, task: DevAITask, patch: dict[str, Any]) -> None:
        """Override to enforce the stage's output contract. Default: no-op."""
        return None

    def _build_result(self, task: DevAITask, patch: dict[str, Any]) -> StageResult:
        """Translate the agent's patch dict → StageResult.

        Default: store the patch under `<role_key>_output` and surface a few
        well-known fields (issue numbers, PR numbers, etc.) at the top
        level so other stages and the dashboard see them.
        """
        data: dict[str, Any] = {f"{self.role_key()}_output": patch}

        # Surface the agent's A2A traffic into the handover bag. base_agent
        # merges inbox + outbox into a cumulative `a2a_messages` list (the
        # prior bag's copy was passed in via _build_state), so writing it
        # back replaces the bag's list with the superset — this is what
        # makes REAL agent-to-agent traffic visible on blueprint runs.
        if isinstance(patch.get("a2a_messages"), list):
            data["a2a_messages"] = patch["a2a_messages"]

        # Surface known scalars at the top level — these are read by
        # condition expressions, downstream stage briefs, and the dashboard.
        # `stories` and `technical_plan` matter most: without them the
        # implement stage briefed "Story #None" with an empty plan (the PR
        # title/body interpolated None) because only the issue NUMBERS
        # survived the handover, not the story content.
        for key in (
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
        ):
            if key in patch and patch[key] is not None:
                data[key] = patch[key]

        # Mirror into DevAITask's structural fields where applicable.
        if isinstance(patch.get("epic_issue_number"), int):
            task.epic_issue_number = patch["epic_issue_number"]
        if isinstance(patch.get("pr_number"), int):
            task.pr_number = patch["pr_number"]
        if isinstance(patch.get("branch_name"), str):
            task.branch_name = patch["branch_name"]
        if isinstance(patch.get("story_issue_numbers"), list):
            task.story_issue_numbers = list(patch["story_issue_numbers"])

        return StageResult(
            next_state=self._next_state(),
            message=patch.get("summary") or patch.get("implementation_summary") or "",
            data=data,
        )


def _safe_agent(factory: Callable[[Any, Any, Any, Any], Any], deps: StageDeps) -> Any | None:
    """Try to construct an agent. Returns None if required deps are missing.

    Most existing agents require both `scm` and `state_manager`. If either
    is None we return None so the adapter short-circuits gracefully.
    """
    if deps.scm is None or deps.state_manager is None:
        return None
    try:
        # Build with the per-run config (the triggering user's overlay when
        # present), so the agent's LLM uses the user's keys/model.
        config = current_agent_config(deps.config)
        return factory(deps.scm, deps.state_manager, config, deps.event_bus)
    except Exception:  # noqa: BLE001 — agent construction is allowed to fail in tests
        logger.exception("agent construction failed")
        return None


__all__ = ["AgentAdapter", "_safe_agent"]
