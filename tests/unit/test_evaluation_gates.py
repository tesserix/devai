from __future__ import annotations

from typing import Any

import pytest

from devai.evaluations.gates import AgentGateService, AgentPublishGate
from devai.evaluations.models import (
    ArtifactVersionRef,
    ComparisonAxis,
    ComparisonCase,
    ComparisonMetric,
    EvalThresholds,
    EvaluationComparison,
    ResolvedEvaluation,
)
from devai.identity import Principal


def _manifest() -> dict[str, Any]:
    return {
        "apiVersion": "registry.agentic.dev/v1alpha1",
        "kind": "Agent",
        "metadata": {"name": "release-agent", "annotations": {}},
        "spec": {
            "title": "Release agent",
            "model": {"provider": "anthropic", "name": "claude-sonnet-4-6"},
            "systemPrompt": "Write accurate release notes.",
            "limits": {"maxTurns": 8, "timeoutSeconds": 900},
            "riskLevel": "medium",
            "evalSuite": {"ref": "release-golden", "version": "3"},
        },
    }


def _run(*, passed: bool = False) -> dict[str, Any]:
    manifest = _manifest()
    return {
        "id": "eval-candidate",
        "agent": "release-agent",
        "configuration": {"draft": manifest},
        "dataset": {"name": "release-cases", "version": "7"},
        "suite": {"name": "release-golden", "version": "3"},
        "summary": {
            "pass_rate": 1.0 if passed else 0.5,
            "p95_latency_ms": 1200 if passed else 4000,
            "cost_usd": 0.02 if passed else 0.06,
            "dimensions": {
                "safety": {"average": 1.0 if passed else 0.8},
                "hallucination": {"average": 0.0 if passed else 0.03},
            },
        },
        "results": [
            {
                "name": "refund-policy",
                "passed": passed,
                "failures": [] if passed else ["invented a refund window"],
            }
        ],
    }


class _Database:
    def __init__(self, runs: dict[tuple[str, str], dict[str, Any]]) -> None:
        self.runs = runs

    async def get_eval_run_by_id(self, owner_scope: str, run_id: str) -> dict[str, Any] | None:
        return self.runs.get((owner_scope, run_id))


class _Evaluations:
    def __init__(self, comparison: EvaluationComparison | None = None) -> None:
        self.comparison = comparison

    async def resolve_suite(self, principal: Principal, ref: ArtifactVersionRef) -> ResolvedEvaluation:
        assert principal.user_scope_id == "tenant-a:alice"
        assert ref == ArtifactVersionRef(name="release-golden", version="3")
        return ResolvedEvaluation(
            cases=[],
            dataset=ArtifactVersionRef(name="release-cases", version="7"),
            suite=ref,
            thresholds=EvalThresholds(
                success=0.95,
                safety=1.0,
                hallucination=0.02,
                p95_latency_s=3,
                cost_per_run_usd=0.05,
            ),
        )

    async def create_comparison(self, principal: Principal, request: Any) -> EvaluationComparison:
        assert principal.user_scope_id == "tenant-a:alice"
        assert request.baseline_run_id == "eval-baseline"
        assert request.candidate_run_id == "eval-candidate"
        assert self.comparison is not None
        return self.comparison


def _principal(*, roles: list[str] | None = None) -> Principal:
    return Principal(
        email="alice@example.com",
        uid="alice",
        tenant_id="tenant-a",
        roles=roles or [],
    )


@pytest.mark.asyncio
async def test_gate_blocks_threshold_failures_and_names_exact_cases() -> None:
    service = AgentGateService(
        database=_Database({("tenant-a:alice", "eval-candidate"): _run()}),
        evaluations=_Evaluations(),
    )

    gate = await service.evaluate(_principal(), _manifest(), "eval-candidate")

    assert gate.status == "blocked"
    assert gate.failing_cases == ["refund-policy"]
    assert set(gate.failing_thresholds) == {
        "success",
        "safety",
        "hallucination",
        "p95_latency_s",
        "cost_per_run_usd",
    }


@pytest.mark.asyncio
async def test_gate_passes_only_the_owner_run_for_the_exact_draft_and_suite() -> None:
    service = AgentGateService(
        database=_Database({("tenant-a:alice", "eval-candidate"): _run(passed=True)}),
        evaluations=_Evaluations(),
    )

    gate = await service.evaluate(_principal(), _manifest(), "eval-candidate")
    hidden = await service.evaluate(
        Principal(email="bob@example.com", uid="bob", tenant_id="tenant-b"),
        _manifest(),
        "eval-candidate",
    )
    changed = _manifest()
    changed["spec"]["systemPrompt"] = "A different untested prompt."
    stale = await service.evaluate(_principal(), changed, "eval-candidate")

    assert gate.status == "passed"
    assert gate.candidate_run_id == "eval-candidate"
    assert hidden.status == "blocked"
    assert hidden.issues == ["owned evaluation run not found"]
    assert stale.status == "blocked"
    assert stale.issues == ["evaluation run does not match the agent draft"]


@pytest.mark.asyncio
async def test_gate_blocks_regression_against_the_published_baseline() -> None:
    comparison = EvaluationComparison(
        id="cmp-1",
        baseline_run_id="eval-baseline",
        candidate_run_id="eval-candidate",
        dataset={"name": "release-cases", "version": "7"},
        metrics={
            "success": ComparisonMetric(baseline=1.0, candidate=1.0, delta=0.0, percent_delta=0.0),
            "cost_usd": ComparisonMetric(baseline=0.01, candidate=0.02, delta=0.01, percent_delta=100.0),
        },
        axes={
            "model": ComparisonAxis(baseline="claude", candidate="claude", changed=False),
        },
        changed_cases=[ComparisonCase(case_id="refund-policy", baseline_passed=True, candidate_passed=False)],
        regressions=[ComparisonCase(case_id="refund-policy", baseline_passed=True, candidate_passed=False)],
        newly_passing=[],
        sample_size=1,
        caveat="Small sample.",
        summary="Candidate regressed.",
        created_at="2026-08-19T00:00:00+00:00",
    )
    service = AgentGateService(
        database=_Database({("tenant-a:alice", "eval-candidate"): _run(passed=True)}),
        evaluations=_Evaluations(comparison),
    )

    gate = await service.evaluate(
        _principal(),
        _manifest(),
        "eval-candidate",
        baseline_run_id="eval-baseline",
    )

    assert gate.status == "blocked"
    assert gate.comparison_id == "cmp-1"
    assert gate.failing_cases == ["refund-policy"]
    assert gate.failing_thresholds["baseline.cost_usd"] == "candidate 0.02 exceeds baseline 0.01"


@pytest.mark.asyncio
async def test_override_requires_verified_admin_and_durable_audit() -> None:
    events: list[dict[str, Any]] = []

    async def audit(**event: Any) -> None:
        events.append(event)

    service = AgentGateService(
        database=_Database({}),
        evaluations=_Evaluations(),
        audit=audit,
    )
    blocked = AgentPublishGate(
        agent_name="release-agent",
        status="blocked",
        suite=ArtifactVersionRef(name="release-golden", version="3"),
        candidate_run_id="eval-candidate",
        failing_cases=["refund-policy"],
    )

    with pytest.raises(PermissionError, match="admin"):
        await service.override(_principal(), blocked, reason="Known judge outage")
    overridden = await service.override(
        _principal(roles=["admin"]),
        blocked,
        reason="Known judge outage",
    )

    assert overridden.status == "overridden"
    assert overridden.approver == "tenant-a:alice"
    assert overridden.override_reason == "Known judge outage"
    assert events[0]["action"] == "agent.eval_gate.override"
    assert events[0]["actor"] == "tenant-a:alice"
    assert events[0]["details"]["failing_cases"] == ["refund-policy"]


@pytest.mark.asyncio
async def test_risk_approval_requires_verified_admin_reason_and_durable_audit() -> None:
    events: list[dict[str, Any]] = []

    async def audit(**event: Any) -> None:
        events.append(event)

    service = AgentGateService(database=_Database({}), evaluations=_Evaluations(), audit=audit)

    with pytest.raises(PermissionError, match="admin"):
        await service.approve_risk(_principal(), agent_name="release-agent", risk_level="high", reason="Reviewed")
    approver = await service.approve_risk(
        _principal(roles=["platform-admin"]),
        agent_name="release-agent",
        risk_level="high",
        reason="Reviewed tool and data boundaries",
    )

    assert approver == "tenant-a:alice"
    assert events == [
        {
            "action": "agent.risk_gate.approve",
            "actor": "tenant-a:alice",
            "actor_type": "user",
            "agent_name": "release-agent",
            "entity_type": "agent",
            "entity_ref": "release-agent",
            "details": {"risk_level": "high", "reason": "Reviewed tool and data boundaries"},
        }
    ]


@pytest.mark.asyncio
async def test_gate_blocks_a_run_that_is_still_running() -> None:
    run = _run(passed=True)
    run["summary"]["status"] = "running"
    service = AgentGateService(
        database=_Database({("tenant-a:alice", "eval-candidate"): run}),
        evaluations=_Evaluations(),
    )

    gate = await service.evaluate(_principal(), _manifest(), "eval-candidate")

    assert gate.status == "blocked"
    assert "evaluation run is running, not completed" in gate.issues
