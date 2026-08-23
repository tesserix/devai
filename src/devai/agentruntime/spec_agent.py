"""``SpecAgent`` — runs a YAML ``Specialization`` as an Agent.

A ``Specialization`` is the data shape every role YAML parses into (prompt,
``allowed_tools``, ``handover_schema``, ``risk_level``, an optional
``legacy_python_class`` bridge). It has historically had *no* execute method —
the long-missing ``SpecializationService.invoke()`` the Job entrypoint still
catches an ``AttributeError`` for. ``SpecAgent`` is that method, finally, and it
satisfies the ``Agent`` protocol so the dispatcher runs a YAML role exactly like
any other agent — inline, as a Job, or recursively.

Two paths, one contract:

  - ``legacy_python_class`` set → delegate to :class:`LegacyAgent` (the spec is a
    thin wrapper over an existing ``BaseAgent``). This is how the 13 ALM + 7 SRE
    roles ship today without rewriting Python.
  - YAML-only → run the canonical tool-calling loop in
    :class:`devai.agentruntime.runner.AgentRunner` against ``deps.llm``, with the
    spec's ``allowed_tools`` as the gate. This is the zero-code custom-agent path.

The two YAML tool-loops that exist today (``AgentRunner`` and the
``_run_yaml_runner`` method inside the run_specialization stage) are *not* merged
here yet — ``SpecAgent`` standardizes on ``AgentRunner``, the cleaner of the two.
Folding ``_run_yaml_runner``'s extras (skill-profile injection, per-role provider
pinning) into ``AgentRunner`` is the next phase; until then those are the only
behaviors a SpecAgent-routed YAML role does not yet get, and the gap is noted at
the call site rather than silently dropped.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from devai.agentruntime.agent import AgentResult, RunContext
from devai.agentruntime.legacy import LegacyAgent
from devai.agentruntime.runner import AgentRunner

if TYPE_CHECKING:
    from devai.pipeline.types import TaskState
    from devai.specializations.base import RiskLevel, Specialization

logger = logging.getLogger(__name__)


class SpecAgent:
    """Execute one ``Specialization`` against the Agent SDK contract."""

    def __init__(self, spec: Specialization) -> None:
        self.spec = spec
        self.name: str = spec.name
        self.output_key: str = spec.output_key or f"{spec.name}_output"

    async def run(self, ctx: RunContext) -> AgentResult:
        # Bridge mode: the spec names an existing Python class. Reuse the one
        # LegacyAgent shim so there is a single construction + mapping path.
        if self.spec.uses_legacy_runtime:
            legacy = LegacyAgent.from_dotted(
                self.spec.legacy_python_class,
                name=self.spec.name,
                output_key=self.output_key,
            )
            result = await legacy.run(ctx)
            # Apply the spec's risk gate even on the bridge path.
            result.next_state = result.next_state or _risk_to_state(self.spec.risk_level)
            return result

        # YAML-only mode: run the canonical tool-calling loop with the spec's
        # per-role provider pin (spec.llm_provider → a specific backend, built
        # from the triggering user's overlay, falling back to the dispatcher-
        # resolved per-principal LLM) and skill-profile guidance layered on.
        runner = AgentRunner(ctx.deps)
        rr = await runner.run(
            self.spec,
            ctx.task,
            extra_context=ctx.extra_context,
            instruction=ctx.instruction,
            llm=await _resolve_spec_llm(self.spec, ctx),
            system_suffix=_skill_guidance(self.spec, ctx),
        )
        return AgentResult(
            ok=not rr.error,
            handover=rr.patch,
            output_key=self.output_key,
            message=f"{self.spec.name} ran via LLM ({self.spec.llm_provider.value})",
            next_state=_risk_to_state(self.spec.risk_level),
            turns=rr.turns,
            tool_calls=rr.tool_calls,
            prompt_tokens=rr.prompt_tokens,
            completion_tokens=rr.completion_tokens,
            trace_steps=rr.trace_steps,
            stub=rr.stub,
            error=rr.error,
            final_text=rr.final_text,
        )


def _risk_to_state(risk: RiskLevel) -> TaskState | None:
    """High/critical-risk roles park the run in AWAITING_APPROVAL for a human
    gate after producing output; lower risk holds the current state. Mirrors
    ``_RunSpecializationStage._next_state``."""
    if risk.needs_human_gate:
        from devai.pipeline.types import TaskState

        return TaskState.AWAITING_APPROVAL
    return None


async def _resolve_spec_llm(spec: Specialization, ctx: RunContext) -> Any:
    """The adapter a YAML spec runs on, three layers (ported from the old
    ``_RunSpecializationStage._select_llm``):

      1. per-tenant base — the dispatcher already resolved ``ctx.llm`` from the
         triggering user's connector (their keys), falling back to platform;
      2. per-role pin — ``spec.llm_provider`` routes to a specific backend
         (claude→anthropic, gemini→vertex_gemini, …), built from the SAME
         settings source so user keys still apply, chained behind the base so a
         provider outage degrades to the base instead of failing the run;
      3. per-request — ``llm_model``/``temperature``/``max_tokens`` ride on each
         request inside the runner.

    Never raises; any resolution failure falls back to the base adapter.
    """
    base = ctx.llm
    wanted = getattr(spec, "llm_provider", None)
    name = getattr(wanted, "value", "") or (str(wanted) if wanted else "")
    try:
        from devai.adapters.llm.factory import create_llm_adapter, resolve_spec_provider

        provider = resolve_spec_provider(name)
        if provider is None or base is None:
            return base
        if getattr(base, "provider_name", "") == provider:
            return base  # already on the requested backend

        deps = ctx.deps
        src = deps.config
        if deps.llm_resolver is not None and ctx.triggered_by:
            from devai.identity import Principal

            principal = Principal.from_dict(ctx.task.principal) or Principal(email=ctx.triggered_by)
            if hasattr(deps.llm_resolver, "settings_for_principal"):
                src = await deps.llm_resolver.settings_for_principal(principal)
            else:
                src = await deps.llm_resolver.settings_for_email(ctx.triggered_by)

        adapter = create_llm_adapter(src, provider=provider)
        if getattr(adapter, "provider_name", "") == "noop":
            # Spec asked for a backend that isn't configured here — run on the
            # base rather than answering canned text.
            logger.warning("spec %s: llm_provider=%s not configured — using default adapter", spec.name, name)
            return base

        from devai.adapters.llm.fallback import FallbackLLMAdapter

        chained = FallbackLLMAdapter(adapter, base)
        # Trial users stay metered even when the spec pins a provider — else a
        # YAML role would be a free side door to the shared platform keys.
        from devai.settings.trial import TrialLLMAdapter, get_trial_meter

        if isinstance(base, TrialLLMAdapter) and ctx.triggered_by:
            return TrialLLMAdapter(chained, get_trial_meter(deps.config), ctx.triggered_by)
        return chained
    except Exception:  # noqa: BLE001
        logger.warning("spec %s: provider selection failed — using default", spec.name, exc_info=True)
        return base


def _skill_guidance(spec: Specialization, ctx: RunContext) -> str:
    """Skill-profile guidance appended to the system prompt (ported from
    ``_RunSpecializationStage._render_skill_guidance``).

    Picks the ``render_for_*`` view that matches the role — name hints win over
    the category so e.g. an orchestration-category release role still gets
    release guidance — and falls back to the planner view. An enhancer, never a
    blocker: any failure yields no guidance.
    """
    try:
        from devai.agents.skills import get_skill_profile

        profile = get_skill_profile(ctx.agent_context.get("skill_profile_name"))
        role = (spec.name or "").lower()
        category = (spec.category or "").lower()
        by_hint = (
            (("qa", "test"), "render_for_qa"),
            (("security",), "render_for_security"),
            (("release", "deploy", "promot"), "render_for_release"),
            (("db_", "database"), "render_for_db_engineer"),
            (("infra", "provision"), "render_for_infra"),
            (("review",), "render_for_reviewer"),
        )
        method = ""
        for hints, renderer in by_hint:
            if any(h in role for h in hints):
                method = renderer
                break
        if not method:
            method = {"coding": "render_for_developer", "review": "render_for_reviewer"}.get(
                category, "render_for_planner"
            )
        render = getattr(profile, method, None) or profile.render_for_planner
        return render() or ""
    except Exception:  # noqa: BLE001 — guidance is an enhancer, never a blocker
        logger.debug("skill guidance failed for %s", spec.name, exc_info=True)
        return ""


__all__ = ["SpecAgent"]
