"""`run_specialization` stage — runs a YAML specialization.

Two modes, both routed through the Agent SDK (`devai.agentruntime`):

1. **Legacy bridge** — the spec has `legacy_python_class:` set. The stage runs
   that class via `LegacyAgent` (the one shim that replaced the reflection that
   used to live here), validates the handover dict against `spec.handover_schema`,
   and writes the result under `spec.output_key`. This is how every existing
   DevAI agent plugs in without rewriting Python code.

2. **YAML-only** — the spec has no `legacy_python_class`. The stage runs it via
   `SpecAgent` → `AgentRunner`: the bounded tool-calling loop with per-role
   provider pinning + skill-profile guidance, the same path a crew member or a
   Job-dispatched agent takes. The stage adds handover validation + the
   output-key / `<name>_text` writes.

Usage from a blueprint:

    stages:
      - name: analyze-requirements
        type: agentic
        stage: run_specialization
        config:
          specialization: requirements_analyst
"""

from __future__ import annotations

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

        if spec.uses_legacy_runtime:
            return await self._run_legacy_bridge(spec, task)

        return await self._run_yaml_spec(spec, task)

    # ── Legacy bridge ────────────────────────────────────────────────

    async def _run_legacy_bridge(self, spec: Specialization, task: DevAITask) -> StageResult:
        """Run the Python class declared in spec.legacy_python_class via the
        Agent SDK's ``LegacyAgent`` — the one shim that replaces the reflection
        that used to live here (and in the orchestrator + Job entrypoint).

        Behavior-identical to the previous reflection bridge: the agent is
        constructed against the platform ``deps.config``/``deps.scm`` (passed as
        explicit dispatcher overrides, so no per-principal resolution changes
        the construction). The handover validation + output-key write + scalar
        mirroring below are unchanged.
        """
        from devai.agentruntime import AgentDispatcher, LegacyAgent

        if self.deps.scm is None or self.deps.state_manager is None:
            return StageResult(
                next_state=self._next_state(spec),
                message=f"{spec.name} skipped — runtime deps unavailable",
                data={spec.output_key: {f"{spec.name}_stub": True}},
            )

        agent = LegacyAgent.from_dotted(spec.legacy_python_class, name=spec.name, output_key=spec.output_key)
        dispatcher = AgentDispatcher(self.deps)
        result = await dispatcher.dispatch(agent, task, config=self.deps.config, scm=self.deps.scm)

        # Import/construction failure → the SDK degrades to a stub result.
        if result.stub:
            return StageResult(
                next_state=self._next_state(spec),
                message=f"{spec.name} skipped — class unavailable",
                data={spec.output_key: {f"{spec.name}_stub": True, "reason": result.error or "unavailable"}},
            )

        patch = result.handover
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

    # ── YAML runner (no legacy class) ─────────────────────────────────

    async def _run_yaml_spec(self, spec: Specialization, task: DevAITask) -> StageResult:
        """Execute a pure-YAML specialization via the unified ``SpecAgent``.

        The whole tool-calling loop — per-role provider pinning, skill-profile
        guidance, the bounded LLM + tool loop (on the one ``ToolDispatcher``
        execution layer), and handover extraction — now lives in the Agent SDK
        (``SpecAgent`` → ``AgentRunner``), the same path a crew member or a
        Job-dispatched agent takes. This stage only adds what is stage-specific:
        handover-schema validation (with ``strict_handover``) and the
        output-key / ``<name>_text`` writes into the handover bag.
        """
        from devai.agentruntime import AgentDispatcher, SpecAgent

        result = await AgentDispatcher(self.deps).dispatch(SpecAgent(spec), task)
        patch = result.handover

        # A degraded (no-LLM) run returns a stub patch — don't validate it.
        if not result.stub:
            violations = validate_handover(spec, patch)
            if violations:
                msg = "; ".join(str(v) for v in violations)
                if self.strict_handover:
                    from devai.specializations.validator import HandoverValidationError

                    raise HandoverValidationError(spec.name, violations)
                logger.warning("specialization %s: handover violations: %s", spec.name, msg)

        data: dict[str, Any] = {spec.output_key: patch}
        if result.final_text and f"{spec.name}_text" not in data:
            data[f"{spec.name}_text"] = result.final_text
        provider = spec.llm_provider.value if hasattr(spec.llm_provider, "value") else spec.llm_provider
        return StageResult(
            next_state=result.next_state or self._next_state(spec),
            message=f"{spec.name} ran via LLM ({provider})",
            data=data,
        )

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


def run_specialization_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    """Stage factory registered with the StageRegistry."""
    return _RunSpecializationStage(deps, config)


__all__ = ["run_specialization_stage"]
