from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from devai.evaluations.models import (
    ArtifactVersionRef,
    ComparisonCreate,
    EvalThresholds,
    EvaluationComparison,
    ResolvedEvaluation,
)
from devai.identity import Principal


class GateDatabase(Protocol):
    async def get_eval_run_by_id(self, owner_scope: str, run_id: str) -> dict[str, Any] | None: ...


class GateEvaluations(Protocol):
    async def resolve_suite(self, principal: Principal, ref: ArtifactVersionRef) -> ResolvedEvaluation: ...

    async def create_comparison(
        self,
        principal: Principal,
        request: ComparisonCreate,
    ) -> EvaluationComparison: ...


AuditWriter = Callable[..., Awaitable[None]]

EVAL_GATE_ANNOTATION_PREFIX = "devai.tesserix.app/eval-"
EVAL_RUN_ANNOTATION = f"{EVAL_GATE_ANNOTATION_PREFIX}run-id"
EVAL_BASELINE_ANNOTATION = f"{EVAL_GATE_ANNOTATION_PREFIX}baseline-run-id"
EVAL_COMPARISON_ANNOTATION = f"{EVAL_GATE_ANNOTATION_PREFIX}comparison-id"
EVAL_SUITE_ANNOTATION = f"{EVAL_GATE_ANNOTATION_PREFIX}suite"
EVAL_APPROVER_ANNOTATION = f"{EVAL_GATE_ANNOTATION_PREFIX}approver"
EVAL_OVERRIDE_REASON_ANNOTATION = f"{EVAL_GATE_ANNOTATION_PREFIX}override-reason"
EVAL_GATED_AT_ANNOTATION = f"{EVAL_GATE_ANNOTATION_PREFIX}gated-at"
EVAL_GATE_LABEL = "devai.tesserix.app/eval-gate"
LIFECYCLE_LABEL = "devai.tesserix.app/lifecycle"


class AgentPublishGate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    agent_name: str
    status: Literal["passed", "blocked", "overridden"]
    suite: ArtifactVersionRef
    candidate_run_id: str
    baseline_run_id: str | None = None
    comparison_id: str | None = None
    failing_cases: list[str] = Field(default_factory=list)
    failing_thresholds: dict[str, str] = Field(default_factory=dict)
    issues: list[str] = Field(default_factory=list)
    approver: str | None = None
    override_reason: str | None = None
    evaluated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class AgentGateService:
    def __init__(
        self,
        *,
        database: GateDatabase,
        evaluations: GateEvaluations,
        audit: AuditWriter | None = None,
    ) -> None:
        self._database = database
        self._evaluations = evaluations
        self._audit = audit

    async def evaluate(
        self,
        principal: Principal,
        manifest: dict[str, Any],
        candidate_run_id: str,
        *,
        baseline_run_id: str | None = None,
    ) -> AgentPublishGate:
        agent_name, suite = self._manifest_gate(manifest)
        owner_scope = principal.user_scope_id
        if not owner_scope:
            return self._blocked(agent_name, suite, candidate_run_id, "owned evaluation run not found")
        candidate = await self._database.get_eval_run_by_id(owner_scope, candidate_run_id)
        if candidate is None:
            return self._blocked(agent_name, suite, candidate_run_id, "owned evaluation run not found")
        issue = self._candidate_issue(candidate, manifest, agent_name, suite)
        if issue:
            return self._blocked(agent_name, suite, candidate_run_id, issue)

        resolved = await self._evaluations.resolve_suite(principal, suite)
        if candidate.get("dataset") != resolved.dataset.model_dump(mode="json"):
            return self._blocked(agent_name, suite, candidate_run_id, "evaluation run uses a different dataset version")

        summary = dict(candidate.get("summary") or {})
        failing_thresholds = self._threshold_failures(summary, resolved.thresholds)
        failing_cases = self._failing_cases(candidate)
        comparison_id = None
        if baseline_run_id:
            comparison = await self._evaluations.create_comparison(
                principal,
                ComparisonCreate(
                    baseline_run_id=baseline_run_id,
                    candidate_run_id=candidate_run_id,
                ),
            )
            comparison_id = comparison.id
            failing_cases = list(dict.fromkeys([*failing_cases, *(case.case_id for case in comparison.regressions)]))
            failing_thresholds.update(self._comparison_regressions(comparison))

        return AgentPublishGate(
            agent_name=agent_name,
            status="blocked" if failing_cases or failing_thresholds else "passed",
            suite=suite,
            candidate_run_id=candidate_run_id,
            baseline_run_id=baseline_run_id,
            comparison_id=comparison_id,
            failing_cases=failing_cases,
            failing_thresholds=failing_thresholds,
        )

    async def override(
        self,
        principal: Principal,
        gate: AgentPublishGate,
        *,
        reason: str,
    ) -> AgentPublishGate:
        if not {"admin", "platform-admin"}.intersection(principal.roles):
            raise PermissionError("admin role required for an evaluation gate override")
        cleaned_reason = reason.strip()
        if not cleaned_reason:
            raise ValueError("override reason is required")
        if len(cleaned_reason) > 1000:
            raise ValueError("override reason must not exceed 1000 characters")
        if self._audit is None:
            raise RuntimeError("durable audit storage unavailable")
        approver = principal.user_scope_id
        if not approver:
            raise PermissionError("verified approver identity required")
        await self._audit(
            action="agent.eval_gate.override",
            actor=approver,
            actor_type="user",
            agent_name=gate.agent_name,
            entity_type="agent",
            entity_ref=gate.agent_name,
            details={
                "suite": gate.suite.model_dump(mode="json"),
                "candidate_run_id": gate.candidate_run_id,
                "baseline_run_id": gate.baseline_run_id,
                "comparison_id": gate.comparison_id,
                "failing_cases": list(gate.failing_cases),
                "failing_thresholds": dict(gate.failing_thresholds),
                "issues": list(gate.issues),
                "reason": cleaned_reason,
            },
        )
        return gate.model_copy(
            update={
                "status": "overridden",
                "approver": approver,
                "override_reason": cleaned_reason,
            }
        )

    @staticmethod
    def _manifest_gate(manifest: dict[str, Any]) -> tuple[str, ArtifactVersionRef]:
        metadata = manifest.get("metadata")
        spec = manifest.get("spec")
        if not isinstance(metadata, dict) or not isinstance(spec, dict):
            raise ValueError("agent manifest metadata and spec are required")
        name = str(metadata.get("name") or "").strip()
        declared = spec.get("evalSuite")
        if not isinstance(declared, dict):
            raise ValueError("spec.evalSuite must pin a suite name and version")
        return name, ArtifactVersionRef(
            name=str(declared.get("ref") or ""),
            version=str(declared.get("version") or ""),
        )

    @staticmethod
    def _candidate_issue(
        candidate: dict[str, Any],
        manifest: dict[str, Any],
        agent_name: str,
        suite: ArtifactVersionRef,
    ) -> str:
        if str(candidate.get("agent") or "") != agent_name:
            return "evaluation run does not match the agent draft"
        if candidate.get("suite") != suite.model_dump(mode="json"):
            return "evaluation run uses a different suite version"
        configuration = candidate.get("configuration")
        draft = configuration.get("draft") if isinstance(configuration, dict) else None
        draft_spec = draft.get("spec") if isinstance(draft, dict) else None
        if draft_spec != manifest.get("spec"):
            return "evaluation run does not match the agent draft"
        return ""

    @staticmethod
    def _threshold_failures(summary: dict[str, Any], thresholds: EvalThresholds) -> dict[str, str]:
        dimensions = summary.get("dimensions")
        typed_dimensions = dimensions if isinstance(dimensions, dict) else {}
        p95_latency_ms = AgentGateService._number(summary.get("p95_latency_ms"))
        values: dict[str, float | None] = {
            "success": AgentGateService._number(summary.get("pass_rate")),
            "safety": AgentGateService._dimension(typed_dimensions, "safety"),
            "hallucination": AgentGateService._dimension(typed_dimensions, "hallucination"),
            "p95_latency_s": p95_latency_ms / 1000 if p95_latency_ms is not None else None,
            "cost_per_run_usd": AgentGateService._number(summary.get("cost_usd")),
        }
        expected = thresholds.model_dump(mode="json", exclude_none=True)
        failures: dict[str, str] = {}
        for name, threshold in expected.items():
            actual = values.get(name)
            if actual is None:
                failures[name] = "required metric unavailable"
                continue
            maximum = name in {"hallucination", "p95_latency_s", "cost_per_run_usd"}
            failed = actual > float(threshold) if maximum else actual < float(threshold)
            if failed:
                operator = "at most" if maximum else "at least"
                failures[name] = f"actual {actual:g}; required {operator} {float(threshold):g}"
        return failures

    @staticmethod
    def _comparison_regressions(comparison: EvaluationComparison) -> dict[str, str]:
        regressions: dict[str, str] = {}
        lower_is_better = {"cost_usd", "p95_latency_ms", "hallucination"}
        higher_is_better = {"success", "safety"}
        for name, metric in comparison.metrics.items():
            if (name in lower_is_better and metric.candidate > metric.baseline) or (
                name in higher_is_better and metric.candidate < metric.baseline
            ):
                direction = "exceeds" if name in lower_is_better else "is below"
                regressions[f"baseline.{name}"] = (
                    f"candidate {metric.candidate:g} {direction} baseline {metric.baseline:g}"
                )
        return regressions

    @staticmethod
    def _dimension(dimensions: dict[str, Any], name: str) -> float | None:
        value = dimensions.get(name)
        if not isinstance(value, dict):
            return None
        return AgentGateService._number(value.get("average", value.get("pass_rate")))

    @staticmethod
    def _number(value: Any) -> float | None:
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        return float(value)

    @staticmethod
    def _failing_cases(candidate: dict[str, Any]) -> list[str]:
        return [
            str(result.get("name") or "")
            for result in candidate.get("results") or []
            if isinstance(result, dict) and not bool(result.get("passed")) and result.get("name")
        ]

    @staticmethod
    def _blocked(
        agent_name: str,
        suite: ArtifactVersionRef,
        candidate_run_id: str,
        issue: str,
    ) -> AgentPublishGate:
        return AgentPublishGate(
            agent_name=agent_name,
            status="blocked",
            suite=suite,
            candidate_run_id=candidate_run_id,
            issues=[issue],
        )


__all__ = [
    "AgentGateService",
    "AgentPublishGate",
    "EVAL_APPROVER_ANNOTATION",
    "EVAL_BASELINE_ANNOTATION",
    "EVAL_COMPARISON_ANNOTATION",
    "EVAL_GATE_ANNOTATION_PREFIX",
    "EVAL_GATED_AT_ANNOTATION",
    "EVAL_GATE_LABEL",
    "EVAL_OVERRIDE_REASON_ANNOTATION",
    "EVAL_RUN_ANNOTATION",
    "EVAL_SUITE_ANNOTATION",
    "LIFECYCLE_LABEL",
]
