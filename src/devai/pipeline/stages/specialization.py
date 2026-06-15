"""`run_specialization` stage — runs a YAML specialization.

Two modes:

1. **Legacy bridge** — the spec has `legacy_python_class:` set. The stage
   imports the class, constructs it, calls `agent.run(state)`, validates
   the handover dict against `spec.handover_schema`, and writes the
   result under `spec.output_key`. This is how every existing DevAI
   agent gets plugged in without rewriting Python code.

2. **YAML-only** — the spec has no `legacy_python_class`. The stage
   would dispatch directly to the LLM provider. The Runner that does
   this is the next slice of work; for now YAML-only specs return a
   stub result and log a clear "not implemented yet" message.

Usage from a blueprint:

    stages:
      - name: analyze-requirements
        type: agentic
        stage: run_specialization
        config:
          specialization: requirements_analyst
"""

from __future__ import annotations

import importlib
import logging
from typing import Any

from devai.pipeline.interfaces import PipelineStage, StageDeps
from devai.pipeline.types import DevAITask, StageResult, TaskState
from devai.specializations.base import RiskLevel, Specialization
from devai.specializations.validator import validate_handover

logger = logging.getLogger(__name__)


class _RunSpecializationStage(PipelineStage):
    def __init__(self, deps: StageDeps, config: dict[str, str]) -> None:
        self.deps = deps
        self.config = config
        self.spec_name = config.get("specialization") or config.get("name") or ""
        if not self.spec_name:
            raise ValueError("run_specialization requires `config.specialization: <name>` in the blueprint YAML")
        # Optional handover-validation strictness (defaults to logging the
        # violations without raising — keeps the pipeline flowing while we
        # debug schema mismatches).
        self.strict_handover = (config.get("strict_handover") or "false").lower() == "true"
        # Lazy spec resolution — registry might not be ready yet at factory time.
        self._spec: Specialization | None = None

    def name(self) -> str:
        return f"run_specialization:{self.spec_name}"

    # ── Stage execution ───────────────────────────────────────────────

    async def execute(self, task: DevAITask) -> StageResult:
        spec = self._resolve_spec()
        if spec is None:
            return StageResult(
                message=f"specialization {self.spec_name!r} not found",
                data={f"{self.spec_name}_error": "not_in_catalog"},
            )

        # Validate that prior stages wrote the keys this role expects.
        missing_inputs = [key for key in spec.context_keys if key not in task.agent_context and key != "requirements"]
        if missing_inputs:
            logger.warning(
                "specialization %s: missing expected context_keys %s",
                spec.name,
                missing_inputs,
            )

        if spec.legacy_python_class:
            return await self._run_legacy_bridge(spec, task)

        return await self._run_yaml_runner(spec, task)

    # ── Legacy bridge ────────────────────────────────────────────────

    async def _run_legacy_bridge(self, spec: Specialization, task: DevAITask) -> StageResult:
        """Construct the Python class declared in spec.legacy_python_class
        and call its run() method.

        This mirrors what AgentAdapter does in stages/_base.py — kept
        separate so the specialization layer can evolve without touching
        the original adapter. Once every adapter is replaced by spec
        invocations, the old adapter goes away.
        """
        try:
            module_path, class_name = spec.legacy_python_class.rsplit(".", 1)
            module = importlib.import_module(module_path)
            klass = getattr(module, class_name)
        except (ImportError, AttributeError, ValueError) as e:
            logger.warning(
                "specialization %s: cannot import %s (%s) — returning stub",
                spec.name,
                spec.legacy_python_class,
                e,
            )
            return StageResult(
                next_state=self._next_state(spec),
                message=f"{spec.name} skipped — class unavailable: {e}",
                data={spec.output_key: {f"{spec.name}_stub": True, "reason": str(e)}},
            )

        if self.deps.scm is None or self.deps.state_manager is None:
            return StageResult(
                next_state=self._next_state(spec),
                message=f"{spec.name} skipped — runtime deps unavailable",
                data={spec.output_key: {f"{spec.name}_stub": True}},
            )

        try:
            agent = klass(
                self.deps.scm,
                self.deps.state_manager,
                self.deps.config,
                self.deps.event_bus,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("specialization %s: agent construction failed", spec.name)
            return StageResult(
                message=f"{spec.name} skipped — construction failed: {e}",
                data={spec.output_key: {f"{spec.name}_stub": True, "reason": str(e)}},
            )

        state = self._build_alm_state(spec, task)
        try:
            patch = await agent.run(state)
        except Exception:
            logger.exception("specialization %s: agent.run raised", spec.name)
            raise

        violations = validate_handover(spec, patch)
        if violations:
            msg = "; ".join(str(v) for v in violations)
            if self.strict_handover:
                from devai.specializations.validator import HandoverValidationError

                raise HandoverValidationError(spec.name, violations)
            logger.warning("specialization %s: handover violations: %s", spec.name, msg)

        data: dict[str, Any] = {spec.output_key: patch}
        # Mirror selected top-level scalars onto the task (legacy contract).
        if isinstance(patch, dict):
            for key in (
                "epic_issue_number",
                "pr_number",
                "branch_name",
                "review_decision",
                "security_decision",
                "build_status",
                "test_failed",
                "test_passed",
                "test_total",
            ):
                if patch.get(key) is not None:
                    data[key] = patch[key]
                    if key == "epic_issue_number" and isinstance(patch[key], int):
                        task.epic_issue_number = patch[key]
                    if key == "pr_number" and isinstance(patch[key], int):
                        task.pr_number = patch[key]
                    if key == "branch_name" and isinstance(patch[key], str):
                        task.branch_name = patch[key]

        return StageResult(
            next_state=self._next_state(spec),
            message=f"{spec.name} ran via {spec.legacy_python_class}",
            data=data,
        )

    # ── LLM selection ─────────────────────────────────────────────────

    async def _select_llm(self, spec: Specialization, task: DevAITask) -> Any:
        """Resolve the adapter this spec runs on (see _run_yaml_runner docs).

        Never raises; any resolution failure falls back to the layer below
        (spec provider → user adapter → platform default → None/stub).
        """
        base = await self.deps.llm_for_principal(task.triggered_by or "")

        wanted = getattr(spec, "llm_provider", None)
        name = getattr(wanted, "value", "") or (str(wanted) if wanted else "")
        try:
            from devai.adapters.llm.factory import create_llm_adapter, resolve_spec_provider

            provider = resolve_spec_provider(name)
            if provider is None or base is None:
                return base
            if getattr(base, "provider_name", "") == provider:
                return base  # already on the requested backend

            # Build from the user's overlay when present so their keys
            # apply even on a role-pinned provider.
            src = self.deps.config
            if self.deps.llm_resolver is not None and task.triggered_by:
                src = await self.deps.llm_resolver.settings_for_email(task.triggered_by)

            cache: dict[str, Any] = self.__dict__.setdefault("_spec_llm_cache", {})
            # The user's identity MUST key the cache: two tenants overlaying
            # the same attr names carry different key VALUES — colliding on
            # attr names alone would serve one tenant's adapter (and API key)
            # to the other.
            who = (task.triggered_by or "").lower() if src is not self.deps.config else ""
            key = f"{provider}|{who}|{','.join(getattr(src, 'overlaid_attrs', []) or [])}"
            adapter = cache.get(key)
            if adapter is None:
                adapter = create_llm_adapter(src, provider=provider)
                if adapter.provider_name == "noop":
                    # Spec asked for a backend that isn't configured here —
                    # run on the default rather than answering canned text.
                    logger.warning(
                        "specialization %s: llm_provider=%s is not configured — using default adapter",
                        spec.name,
                        name,
                    )
                    return base
                if len(cache) > 32:
                    cache.clear()
                cache[key] = adapter
            # Resilience: chain the run's default adapter behind the
            # spec-pinned provider so a provider outage degrades the role
            # to the default backend instead of failing the workflow.
            from devai.adapters.llm.fallback import FallbackLLMAdapter

            chained = FallbackLLMAdapter(adapter, base)
            # Trial users stay metered even when the spec pins a provider —
            # otherwise a YAML role would be a free side door to the shared
            # platform keys.
            from devai.settings.trial import TrialLLMAdapter, get_trial_meter

            if isinstance(base, TrialLLMAdapter) and task.triggered_by:
                return TrialLLMAdapter(chained, get_trial_meter(self.deps.config), task.triggered_by)
            return chained
        except Exception:  # noqa: BLE001
            logger.warning("specialization %s: provider selection failed — using default", spec.name, exc_info=True)
            return base

    # ── YAML runner (no legacy class) ─────────────────────────────────

    async def _run_yaml_runner(self, spec: Specialization, task: DevAITask) -> StageResult:
        """Execute a pure-YAML specialization against the LLM adapter.

        Runs a tool-calling loop: the spec's ``system_prompt`` drives the
        model, ``allowed_tools`` are offered as callable tools (resolved
        from the built-in catalog), and the loop runs until the model
        stops calling tools or ``max_turns`` is hit. The final text is
        parsed for a handover object, validated against
        ``handover_schema``, and written under ``output_key``.

        Degrades to a clear stub when no LLM adapter is wired (e.g. unit
        tests or a noop deployment) so the pipeline never hard-fails.
        """
        # LLM selection, three layers:
        #   1. per-tenant: the triggering user's own connector (Settings
        #      overlay — their keys/provider) beats the platform default;
        #   2. per-role: the spec's `llm_provider:` pins this agent to a
        #      specific backend (claude→anthropic, gemini→vertex_gemini, …),
        #      built from the SAME settings source so user keys still apply;
        #   3. per-request: `llm_model:`/`temperature`/`max_tokens` from the
        #      YAML ride on every LLMRequest below.
        llm = await self._select_llm(spec, task)
        if llm is None:
            logger.info("specialization %s: no LLM adapter wired — returning stub", spec.name)
            return StageResult(
                next_state=self._next_state(spec),
                message=f"{spec.name}: no LLM adapter configured",
                data={spec.output_key: {"stub": True, "reason": "no_llm_adapter", "spec_name": spec.name}},
            )

        from devai.adapters.llm.base import LLMMessage, LLMRequest, LLMRole

        # Allow tests to inject a fake dispatcher; otherwise build the real one.
        dispatcher = (self.deps.extra or {}).get("tool_dispatcher")
        if dispatcher is None:
            from devai.tools.dispatch import ToolDispatcher

            triggered_by = getattr(task, "triggered_by", "") or ""
            # Per-principal SCM: the agent's git tools use the TRIGGERING user's
            # own PAT / GitHub App (Settings → Source Control), not the platform
            # App. Falls back to deps.scm when the user configured nothing.
            scm = self.deps.scm
            try:
                scm = await self.deps.scm_for_principal(triggered_by) or self.deps.scm
            except Exception:  # noqa: BLE001
                pass
            # In a dry run, mutating tools (PR/issue creation, kubectl/argocd
            # writes, paging) are offered to the model but short-circuited.
            dispatcher = ToolDispatcher(
                scm,
                dry_run=getattr(task, "dry_run", False),
                triggered_by=triggered_by,
            )

        tool_specs = dispatcher.build_tool_specs(spec.allowed_tools)
        messages = [LLMMessage(role=LLMRole.USER, content=self._build_user_prompt(spec, task))]

        # Skill-profile parity with the bridged Python agents: YAML-only
        # roles previously ran on spec.system_prompt alone — none of the
        # stack guidance (directory layout, test framework, conventions)
        # ever reached them, silently degrading their output quality.
        # The renderer is picked per role (a coding spec gets developer
        # guidance, a release spec gets release guidance, …) instead of
        # the one-size-fits-all planner view.
        system_prompt = spec.system_prompt
        try:
            from devai.agents.skills import get_skill_profile

            profile = get_skill_profile(task.agent_context.get("skill_profile_name"))
            guidance = self._render_skill_guidance(spec, profile)
            if guidance:
                system_prompt = f"{system_prompt}\n\n{guidance}"
        except Exception:  # noqa: BLE001 — guidance is an enhancer, never a blocker
            logger.debug("skill profile injection failed for %s", spec.name, exc_info=True)

        final_text = ""
        turns = max(1, spec.max_turns or 8)
        for _ in range(turns):
            request = LLMRequest(
                system=system_prompt,
                messages=messages,
                tools=tool_specs,
                model=spec.llm_model or "",
                max_tokens=spec.max_tokens,
                temperature=spec.temperature,
                # Attribution for telemetry + the usage ledger (cost per agent,
                # per user, per run).
                extra={
                    "agent": spec.name,
                    "run_id": task.id,
                    "triggered_by": task.triggered_by or "",
                },
            )
            resp = await llm.generate(request)
            if resp.tool_calls:
                messages.append(LLMMessage(role=LLMRole.ASSISTANT, content=resp.text or ""))
                for call in resp.tool_calls:
                    result = await dispatcher.execute(call.name, call.arguments)
                    messages.append(LLMMessage(role=LLMRole.TOOL, content=result, name=call.name, tool_call_id=call.id))
                continue
            final_text = resp.text or ""
            break

        patch = self._parse_handover(final_text)
        violations = validate_handover(spec, patch)
        if violations:
            msg = "; ".join(str(v) for v in violations)
            if self.strict_handover:
                from devai.specializations.validator import HandoverValidationError

                raise HandoverValidationError(spec.name, violations)
            logger.warning("specialization %s: handover violations: %s", spec.name, msg)

        data: dict[str, Any] = {spec.output_key: patch}
        if f"{spec.name}_text" not in data:
            data[f"{spec.name}_text"] = final_text
        return StageResult(
            next_state=self._next_state(spec),
            message=f"{spec.name} ran via LLM ({spec.llm_provider.value if hasattr(spec.llm_provider, 'value') else spec.llm_provider})",
            data=data,
        )

    @staticmethod
    def _render_skill_guidance(spec: Specialization, profile: Any) -> str:
        """Pick the render_for_* view that matches this role.

        Mirrors how the bridged Python agents choose their renderer
        (senior_developer → developer, qa_tester → qa, …). Name hints win
        over the category so e.g. an orchestration-category release role
        still gets release guidance. Falls back to the planner view.
        """
        name = (spec.name or "").lower()
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
            if any(h in name for h in hints):
                method = renderer
                break
        if not method:
            method = {
                "coding": "render_for_developer",
                "review": "render_for_reviewer",
            }.get(category, "render_for_planner")
        render = getattr(profile, method, None) or profile.render_for_planner
        return render() or ""

    def _build_user_prompt(self, spec: Specialization, task: DevAITask) -> str:
        """Compose the user turn from the task intent plus any context keys
        prior stages handed over (so the role sees its declared inputs)."""
        parts = [f"# Task\n\n{task.intent}"]
        for key in spec.context_keys:
            val = task.agent_context.get(key)
            if val:
                parts.append(f"\n## {key}\n\n{val}")
        if spec.handover_schema:
            fields = ", ".join(sorted(spec.handover_schema.keys()))
            parts.append(
                "\n## Output\n\nReturn a single JSON object with these keys: "
                f"{fields}. Wrap it in a ```json fenced block."
            )
        return "\n".join(parts)

    @staticmethod
    def _parse_handover(text: str) -> dict[str, Any]:
        """Best-effort extraction of the handover JSON object from model text.

        Looks for a ```json fenced block first, then the first balanced
        ``{...}``. Falls back to ``{"text": <raw>}`` so downstream stages
        always get a dict.
        """
        import json
        import re

        if not text:
            return {"text": ""}
        fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        candidate = fence.group(1) if fence else None
        if candidate is None:
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end > start:
                candidate = text[start : end + 1]
        if candidate:
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict):
                    return parsed
            except (ValueError, TypeError):
                pass
        return {"text": text}

    # ── Helpers ───────────────────────────────────────────────────────

    def _resolve_spec(self) -> Specialization | None:
        if self._spec is not None:
            return self._spec
        # The registry lives on either the parent PipelineService OR a
        # standalone SpecializationService. We resolve through whatever
        # was attached to StageDeps.extra or pass-through via deps.config.
        extra = self.deps.extra or {}
        registry = extra.get("specialization_registry")
        if registry is None:
            # Fall back to reading from disk on first use.
            from devai.specializations.registry import SpecializationRegistry

            spec_dir = getattr(self.deps.config, "specializations_dir", "specializations")
            try:
                registry = SpecializationRegistry.from_directory(spec_dir)
            except Exception:  # noqa: BLE001
                logger.exception("Could not load specializations from %s", spec_dir)
                return None

        try:
            self._spec = registry.resolve(self.spec_name)
        except Exception:  # noqa: BLE001
            logger.exception("specialization %s missing from registry", self.spec_name)
            return None
        return self._spec

    def _next_state(self, spec: Specialization) -> TaskState | None:
        """Map a specialization's risk_level → TaskState.

        Roles with HIGH or CRITICAL risk park the pipeline in
        AWAITING_APPROVAL so a human can review before downstream stages
        run. Lower-risk roles just keep the task in RUNNING and the
        executor advances normally.
        """
        if spec.risk_level == RiskLevel.CRITICAL:
            return TaskState.AWAITING_APPROVAL
        if spec.risk_level == RiskLevel.HIGH:
            return TaskState.AWAITING_APPROVAL
        return None  # let the executor hold current state

    def _build_alm_state(self, spec: Specialization, task: DevAITask) -> dict[str, Any]:
        return {
            "run_id": task.id,
            "repo_full_name": task.repo,
            "requirements": task.intent,
            "stage": task.current_stage or self.name(),
            "branch_name": task.branch_name,
            "pr_number": task.pr_number,
            "epic_issue_number": task.epic_issue_number,
            "story_issue_numbers": list(task.story_issue_numbers),
            "trigger_type": task.trigger_type,
            # All handover bag entries from prior stages
            **task.agent_context,
        }


def run_specialization_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    """Stage factory registered with the StageRegistry."""
    return _RunSpecializationStage(deps, config)


__all__ = ["run_specialization_stage"]
