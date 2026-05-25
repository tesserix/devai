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
            raise ValueError(
                "run_specialization requires `config.specialization: <name>` in the blueprint YAML"
            )
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
        missing_inputs = [
            key for key in spec.context_keys if key not in task.agent_context and key != "requirements"
        ]
        if missing_inputs:
            logger.warning(
                "specialization %s: missing expected context_keys %s",
                spec.name,
                missing_inputs,
            )

        if spec.legacy_python_class:
            return await self._run_legacy_bridge(spec, task)

        return self._yaml_only_stub(spec, task)

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
        except Exception as e:  # noqa: BLE001
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

    # ── YAML-only stub ───────────────────────────────────────────────

    def _yaml_only_stub(self, spec: Specialization, task: DevAITask) -> StageResult:
        """Placeholder for specs that have no Python class.

        The future SpecializationRunner will execute these against the
        LLM provider declared in the YAML. For now we surface a clear
        message so blueprints that reference yaml-only roles don't
        silently no-op.
        """
        logger.info(
            "specialization %s is YAML-only — runner not yet implemented, returning stub",
            spec.name,
        )
        return StageResult(
            next_state=self._next_state(spec),
            message=f"{spec.name}: YAML-only role, runner pending",
            data={
                spec.output_key: {
                    "stub": True,
                    "reason": "yaml_only_runner_not_implemented",
                    "spec_name": spec.name,
                },
            },
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
