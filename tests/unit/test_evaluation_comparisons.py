from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import devai.evaluations.routes as evaluation_routes
from devai.adapters.object_store.noop import NoopObjectStoreAdapter
from devai.evaluations.models import ComparisonCreate, EvaluationComparison
from devai.evaluations.service import EvaluationInvalid, EvaluationNotFound, EvaluationService
from devai.identity import Principal


class _Database:
    def __init__(self, runs: list[dict[str, Any]]) -> None:
        self.runs = {(run["owner_scope"], run["id"]): run for run in runs}
        self.comparisons: dict[tuple[str, str], dict[str, Any]] = {}

    async def get_eval_run_by_id(self, owner_scope: str, run_id: str) -> dict[str, Any] | None:
        return self.runs.get((owner_scope, run_id))

    async def create_eval_comparison(self, **values: Any) -> dict[str, Any]:
        row = {**values, "created_at": "2026-08-19T12:00:00+00:00"}
        self.comparisons[(values["owner_scope"], values["id"])] = row
        return row

    async def get_eval_comparison(self, owner_scope: str, comparison_id: str) -> dict[str, Any] | None:
        return self.comparisons.get((owner_scope, comparison_id))


def _run(run_id: str, *, candidate: bool = False) -> dict[str, Any]:
    return {
        "id": run_id,
        "owner_scope": "tenant-a:alice",
        "tenant_id": "tenant-a",
        "user_id": "alice",
        "sandbox_id": f"sb-{run_id}",
        "agent": "support-agent",
        "dataset": {"name": "golden", "version": "3"},
        "suite": {"name": "release-gate", "version": "2"},
        "configuration": {
            "agent": {"name": "support-agent", "version": "13" if candidate else "12"},
            "model": {"provider": "anthropic", "model": "claude-sonnet-4"},
            "prompt": {"ref": "support", "version": "13" if candidate else "12"},
            "tools": {"default_mode": "mock", "overrides": {}},
        },
        "created_at": "2026-08-19T11:00:00+00:00",
        "summary": {
            "cases": 2,
            "pass_rate": 0.5 if not candidate else 0.5,
            "p95_latency_ms": 1800 if not candidate else 2200,
            "cost_usd": 0.018 if not candidate else 0.024,
            "total_tokens": 1000 if not candidate else 1200,
            "dimensions": {
                "groundedness": {"average": 0.93 if not candidate else 0.97},
                "safety": {"average": 1.0},
            },
        },
        "results": [
            {
                "name": "refund",
                "passed": not candidate,
                "invocation_id": f"inv-{run_id}-refund",
                "trace_url": f"/api/traces/inv-{run_id}-refund",
            },
            {
                "name": "status",
                "passed": candidate,
                "invocation_id": f"inv-{run_id}-status",
                "trace_url": f"/api/traces/inv-{run_id}-status",
            },
        ],
    }


async def test_same_dataset_runs_produce_decision_ready_deltas_and_paired_case_traces() -> None:
    database = _Database([_run("eval-baseline"), _run("eval-candidate", candidate=True)])
    service = EvaluationService(database=database, object_store=NoopObjectStoreAdapter())

    comparison = await service.create_comparison(
        Principal(email="alice@example.com", uid="alice", tenant_id="tenant-a"),
        ComparisonCreate(baseline_run_id="eval-baseline", candidate_run_id="eval-candidate"),
    )
    reordered = await service.create_comparison(
        Principal(email="alice@example.com", uid="alice", tenant_id="tenant-a"),
        ComparisonCreate(
            baseline_run_id="eval-baseline",
            candidate_run_id="eval-candidate",
            axes=["tool_config", "agent_version", "model", "prompt_version"],
        ),
    )

    assert reordered.id == comparison.id
    assert comparison.dataset == {"name": "golden", "version": "3"}
    assert comparison.metrics["groundedness"].delta == 0.04
    assert comparison.metrics["p95_latency_ms"].percent_delta == 22.2222
    assert comparison.metrics["cost_usd"].percent_delta == 33.3333
    assert comparison.regressions[0].case_id == "refund"
    assert comparison.newly_passing[0].case_id == "status"
    assert comparison.changed_cases[0].baseline_trace_url == "/api/traces/inv-eval-baseline-refund"
    assert comparison.changed_cases[0].candidate_trace_url == "/api/traces/inv-eval-candidate-refund"
    assert comparison.axes["prompt_version"].changed is True
    assert comparison.axes["model"].changed is False
    assert comparison.sample_size == 2
    assert "small sample" in comparison.caveat.lower()
    assert "cost" in comparison.summary.lower()
    assert "latency" in comparison.summary.lower()


async def test_comparison_rejects_different_dataset_versions_and_hides_foreign_runs() -> None:
    baseline = _run("eval-baseline")
    candidate = _run("eval-candidate", candidate=True)
    candidate["dataset"] = {"name": "golden", "version": "4"}
    database = _Database([baseline, candidate])
    service = EvaluationService(database=database, object_store=NoopObjectStoreAdapter())
    alice = Principal(email="alice@example.com", uid="alice", tenant_id="tenant-a")

    with pytest.raises(EvaluationInvalid, match="same dataset version"):
        await service.create_comparison(
            alice,
            ComparisonCreate(baseline_run_id="eval-baseline", candidate_run_id="eval-candidate"),
        )

    with pytest.raises(EvaluationNotFound, match="evaluation run not found"):
        await service.create_comparison(
            Principal(email="bob@example.com", uid="bob", tenant_id="tenant-a"),
            ComparisonCreate(baseline_run_id="eval-baseline", candidate_run_id="eval-candidate"),
        )


class _ComparisonService:
    def __init__(self, comparison: EvaluationComparison) -> None:
        self.comparison = comparison
        self.created: list[tuple[Principal, ComparisonCreate]] = []

    async def create_comparison(self, principal: Principal, body: ComparisonCreate) -> EvaluationComparison:
        self.created.append((principal, body))
        return self.comparison

    async def get_comparison(self, principal: Principal, comparison_id: str) -> EvaluationComparison:
        del principal
        if comparison_id != self.comparison.id:
            raise EvaluationNotFound(f"comparison {comparison_id} not found")
        return self.comparison


def test_comparison_routes_require_auth_and_return_the_public_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    comparison_service = EvaluationService(
        database=_Database([_run("eval-baseline"), _run("eval-candidate", candidate=True)]),
        object_store=NoopObjectStoreAdapter(),
    )
    principal = Principal(email="alice@example.com", uid="alice", tenant_id="tenant-a")
    comparison = asyncio.run(
        comparison_service.create_comparison(
            principal,
            ComparisonCreate(baseline_run_id="eval-baseline", candidate_run_id="eval-candidate"),
        )
    )
    service = _ComparisonService(comparison)
    app = FastAPI()
    app.include_router(evaluation_routes.comparison_router)
    app.state.evaluation_service = service

    async def authenticated(_request):
        return principal

    monkeypatch.setattr(evaluation_routes, "require_principal", authenticated)
    client = TestClient(app)
    created = client.post(
        "/api/comparisons",
        json={"baseline_run_id": "eval-baseline", "candidate_run_id": "eval-candidate"},
    )
    fetched = client.get(f"/api/comparisons/{comparison.id}")

    assert created.status_code == 201, created.text
    assert fetched.status_code == 200, fetched.text
    assert created.json()["id"] == comparison.id
    assert "owner_scope" not in created.json()
    assert service.created[0][0] == principal

    async def anonymous(_request):
        raise HTTPException(status_code=401, detail="unauthenticated")

    monkeypatch.setattr(evaluation_routes, "require_principal", anonymous)
    assert client.get(f"/api/comparisons/{comparison.id}").status_code == 401
