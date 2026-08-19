"""HTTP layer for running a suite of checks against a sandbox."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from devai.evaluations.models import ArtifactVersionRef, ResolvedEvaluation
from devai.sandbox.evals import EvalRunner, EvalStore
from devai.sandbox.invoke import SandboxInvoker
from devai.sandbox.routes import router
from devai.sandbox.service import SandboxService
from devai.sandbox.trace import TraceStore
from devai.specializations.loader import load_specialization_from_string
from devai.specializations.registry import SpecializationRegistry
from tests.unit.test_sandbox import _FakeDB
from tests.unit.test_sandbox_invoke import _GrantedSandboxLLM, _ScriptedLLM
from tests.unit.test_sandbox_invoke_routes import _MALLORY, _SAM, _SPEC, _YAML, _Specs

_CASES: dict[str, Any] = {
    "cases": [
        {"name": "mentions the notes", "input": "summarise", "expect": {"contains": ["notes"]}},
        {"name": "stays quiet about outages", "input": "summarise", "expect": {"contains": ["outage"]}},
    ]
}


class _Evaluations:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    async def resolve_dataset(self, principal, ref: ArtifactVersionRef) -> ResolvedEvaluation:
        self.calls.append((principal.user_scope_id, "dataset", f"{ref.name}@{ref.version}"))
        from devai.sandbox.evals import EvalCase

        return ResolvedEvaluation(
            cases=[EvalCase.model_validate({"name": "pinned", "input": "summarise", "expect": {}})],
            dataset=ref,
        )

    async def resolve_suite(self, principal, ref: ArtifactVersionRef) -> ResolvedEvaluation:
        self.calls.append((principal.user_scope_id, "suite", f"{ref.name}@{ref.version}"))
        from devai.sandbox.evals import EvalCase

        return ResolvedEvaluation(
            cases=[EvalCase.model_validate({"name": "pinned", "input": "summarise", "expect": {}})],
            dataset=ArtifactVersionRef(name="golden", version="3"),
            suite=ref,
        )


class _FailingEvalDatabase:
    async def get_eval_run(self, *_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("postgres unavailable")

    async def list_eval_runs(self, *_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        raise RuntimeError("postgres unavailable")


def _client(*, evals: bool = True, evaluations: _Evaluations | None = None) -> TestClient:
    from devai.adapters.llm.base import LLMResponse
    from devai.config import Settings
    from devai.pipeline.interfaces import StageDeps

    registry = SpecializationRegistry()
    registry.register(load_specialization_from_string(_YAML))

    app = FastAPI()
    app.state.sandbox_service = SandboxService(_FakeDB())
    app.state.sandbox_traces = TraceStore(None)
    llm = _ScriptedLLM([LLMResponse(text="Here are the notes.")] * 4)
    invoker = SandboxInvoker(
        specializations=_Specs(registry),
        deps=StageDeps(config=Settings(), llm=llm),
        traces=app.state.sandbox_traces,
        credentials=_GrantedSandboxLLM(llm),
    )
    app.state.sandbox_invoker = invoker
    app.state.sandbox_evals = EvalRunner(invoker, EvalStore(None)) if evals else None
    app.state.evaluation_service = evaluations
    app.include_router(router)
    return TestClient(app)


def _sandbox(client: TestClient) -> str:
    return client.post("/api/sandboxes", json=_SPEC, headers=_SAM).json()["id"]


def test_a_suite_answers_with_a_pass_rate_and_per_case_detail() -> None:
    client = _client()
    sid = _sandbox(client)

    r = client.post(f"/api/sandboxes/{sid}/evals", json=_CASES, headers=_SAM)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"].startswith("eval-")
    assert body["summary"] == {**body["summary"], "cases": 2, "passed": 1, "failed": 1}
    assert body["results"][1]["failures"] == ["missing expected text: 'outage'"]


def test_a_run_is_listed_and_readable_afterwards() -> None:
    client = _client()
    sid = _sandbox(client)
    run_id = client.post(f"/api/sandboxes/{sid}/evals", json=_CASES, headers=_SAM).json()["id"]

    listed = client.get(f"/api/sandboxes/{sid}/evals", headers=_SAM)
    one = client.get(f"/api/sandboxes/{sid}/evals/{run_id}", headers=_SAM)

    assert [r["id"] for r in listed.json()] == [run_id]
    assert one.json()["summary"]["cases"] == 2


def test_a_suite_with_no_cases_is_refused() -> None:
    client = _client()
    sid = _sandbox(client)

    assert client.post(f"/api/sandboxes/{sid}/evals", json={"cases": []}, headers=_SAM).status_code == 422


def test_a_malformed_case_is_a_422_not_a_500() -> None:
    client = _client()
    sid = _sandbox(client)

    r = client.post(f"/api/sandboxes/{sid}/evals", json={"cases": [{"name": "x"}]}, headers=_SAM)

    assert r.status_code == 422


def test_another_owner_can_neither_run_nor_read_checks() -> None:
    client = _client()
    sid = _sandbox(client)
    client.post(f"/api/sandboxes/{sid}/evals", json=_CASES, headers=_SAM)

    assert client.post(f"/api/sandboxes/{sid}/evals", json=_CASES, headers=_MALLORY).status_code == 404
    assert client.get(f"/api/sandboxes/{sid}/evals", headers=_MALLORY).status_code == 404


def test_checks_503_until_the_runner_is_wired() -> None:
    client = _client(evals=False)
    sid = _sandbox(client)

    assert client.post(f"/api/sandboxes/{sid}/evals", json=_CASES, headers=_SAM).status_code == 503


def test_durable_eval_history_read_failures_are_explicit_503s() -> None:
    client = _client()
    sid = _sandbox(client)
    client.app.state.sandbox_evals._store = EvalStore(None, database=_FailingEvalDatabase())

    assert client.get(f"/api/sandboxes/{sid}/evals", headers=_SAM).status_code == 503
    assert client.get(f"/api/sandboxes/{sid}/evals/eval-1", headers=_SAM).status_code == 503


def test_a_dataset_reference_resolves_and_is_recorded_on_the_run() -> None:
    evaluations = _Evaluations()
    client = _client(evaluations=evaluations)
    sid = _sandbox(client)

    response = client.post(
        f"/api/sandboxes/{sid}/evals",
        json={"dataset": {"name": "golden", "version": "3"}},
        headers=_SAM,
    )

    assert response.status_code == 200, response.text
    assert response.json()["dataset"] == {"name": "golden", "version": "3"}
    assert response.json()["suite"] is None
    assert evaluations.calls == [("sam@example.com", "dataset", "golden@3")]


def test_a_suite_reference_records_both_the_suite_and_its_exact_dataset_version() -> None:
    evaluations = _Evaluations()
    client = _client(evaluations=evaluations)
    sid = _sandbox(client)

    response = client.post(
        f"/api/sandboxes/{sid}/evals",
        json={"suite": {"name": "release-gate", "version": "2"}},
        headers=_SAM,
    )

    assert response.status_code == 200, response.text
    assert response.json()["dataset"] == {"name": "golden", "version": "3"}
    assert response.json()["suite"] == {"name": "release-gate", "version": "2"}


def test_eval_request_accepts_exactly_one_inline_dataset_or_suite_source() -> None:
    client = _client(evaluations=_Evaluations())
    sid = _sandbox(client)

    both = client.post(
        f"/api/sandboxes/{sid}/evals",
        json={
            "cases": _CASES["cases"],
            "dataset": {"name": "golden", "version": "3"},
        },
        headers=_SAM,
    )

    assert both.status_code == 422
