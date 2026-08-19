from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

import devai.evaluations.routes as evaluation_routes
from devai.adapters.object_store.noop import NoopObjectStoreAdapter
from devai.evaluations.service import EvaluationService
from devai.identity import Principal
from devai.sandbox.evals import CaseResult, EvalRun
from devai.sandbox.models import AgentRef, DatasetRef, ModelRef, SandboxRecord, SandboxSpec, SandboxStatus


class _Database:
    def __init__(self) -> None:
        self.datasets: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.suites: dict[tuple[str, str, str], dict[str, Any]] = {}

    async def create_eval_dataset_version(self, **values: Any) -> dict[str, Any] | None:
        key = (values["owner_scope"], values["name"], values["version"])
        if key in self.datasets:
            return None
        row = {**values, "created_at": "2026-08-19T00:00:00+00:00"}
        self.datasets[key] = row
        return row

    async def list_eval_datasets(self, owner_scope: str, *, limit: int) -> list[dict[str, Any]]:
        return [row for key, row in self.datasets.items() if key[0] == owner_scope][:limit]

    async def get_eval_dataset_version(self, owner_scope: str, name: str, version: str) -> dict[str, Any] | None:
        return self.datasets.get((owner_scope, name, version))

    async def create_eval_suite(self, **values: Any) -> dict[str, Any] | None:
        key = (values["owner_scope"], values["name"], values["version"])
        if key in self.suites:
            return None
        row = {**values, "created_at": "2026-08-19T00:00:00+00:00"}
        self.suites[key] = row
        return row

    async def list_eval_suites(self, owner_scope: str, *, limit: int) -> list[dict[str, Any]]:
        return [row for key, row in self.suites.items() if key[0] == owner_scope][:limit]

    async def get_eval_suite(self, owner_scope: str, name: str, version: str) -> dict[str, Any] | None:
        return self.suites.get((owner_scope, name, version))


ALICE = Principal(email="alice@example.com", uid="alice", tenant_id="tenant-a")
BOB = Principal(email="bob@example.com", uid="bob", tenant_id="tenant-a")


def _app(principal: Principal | None, *, service: Any | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(evaluation_routes.router)
    app.state.evaluation_service = service or EvaluationService(
        database=_Database(),
        object_store=NoopObjectStoreAdapter(),
    )

    async def _principal(_request: Any) -> Principal:
        if principal is None:
            from fastapi import HTTPException

            raise HTTPException(status_code=401, detail="authentication required")
        return principal

    evaluation_routes.require_principal = _principal  # type: ignore[assignment]
    return app


def _dataset_body() -> dict[str, Any]:
    return {
        "name": "golden",
        "version": "3",
        "cases": [{"id": "refund", "input": "Refund order 4471", "tags": ["happy-path"]}],
    }


def test_evaluation_routes_always_require_an_authenticated_principal() -> None:
    client = TestClient(_app(None))

    assert client.get("/api/evaluations/datasets").status_code == 401
    assert client.post("/api/evaluations/datasets", json=_dataset_body()).status_code == 401
    assert (
        client.post(
            "/api/evaluations",
            json={"suite": {"name": "release-gate", "version": "1"}, "sandbox_id": "sb-1"},
        ).status_code
        == 401
    )
    assert client.get("/api/evaluations/eval-1").status_code == 401


def test_dataset_create_list_and_exact_version_get_do_not_expose_owner_identity() -> None:
    client = TestClient(_app(ALICE))

    created = client.post("/api/evaluations/datasets", json=_dataset_body())
    listed = client.get("/api/evaluations/datasets")
    fetched = client.get("/api/evaluations/datasets/golden/versions/3")

    assert created.status_code == 201
    assert listed.status_code == 200
    assert fetched.status_code == 200
    assert fetched.json()["cases"][0]["id"] == "refund"
    assert "owner_scope" not in created.json()
    assert "owner_scope" not in fetched.json()


def test_cross_user_dataset_access_is_indistinguishable_from_missing() -> None:
    service = EvaluationService(database=_Database(), object_store=NoopObjectStoreAdapter())
    alice = TestClient(_app(ALICE, service=service))
    assert alice.post("/api/evaluations/datasets", json=_dataset_body()).status_code == 201

    bob = TestClient(_app(BOB, service=service))

    assert bob.get("/api/evaluations/datasets/golden/versions/3").status_code == 404
    assert bob.get("/api/evaluations/datasets").json() == []


def test_suite_create_and_get_preserve_exact_dataset_version() -> None:
    client = TestClient(_app(ALICE))
    assert client.post("/api/evaluations/datasets", json=_dataset_body()).status_code == 201

    created = client.post(
        "/api/evaluations/suites",
        json={
            "name": "release-gate",
            "version": "2",
            "dataset": {"name": "golden", "version": "3"},
            "scorers": ["exact_match", "expected_tool_call"],
            "thresholds": {"success": 0.95, "safety": 1.0},
        },
    )
    fetched = client.get("/api/evaluations/suites/release-gate/versions/2")

    assert created.status_code == 201
    assert fetched.status_code == 200
    assert fetched.json()["dataset"] == {"name": "golden", "version": "3"}


def test_suite_create_rejects_an_unknown_scorer_before_persisting_it() -> None:
    client = TestClient(_app(ALICE))
    assert client.post("/api/evaluations/datasets", json=_dataset_body()).status_code == 201

    response = client.post(
        "/api/evaluations/suites",
        json={
            "name": "bad-suite",
            "version": "1",
            "dataset": {"name": "golden", "version": "3"},
            "scorers": ["not_registered"],
        },
    )

    assert response.status_code == 422
    assert "unknown scorer" in response.text


def test_dataset_create_rejects_an_oversized_aggregate_case_payload() -> None:
    client = TestClient(_app(ALICE))
    body = {
        "name": "too-large",
        "version": "1",
        "cases": [{"id": f"case-{index}", "input": "x" * 100_000} for index in range(11)],
    }

    response = client.post("/api/evaluations/datasets", json=body)

    assert response.status_code == 422
    assert "dataset case payload" in response.text


class _RunStore:
    def __init__(self) -> None:
        self.runs: dict[tuple[str, str], EvalRun] = {}

    async def get_by_id(self, owner_scope: str, run_id: str) -> EvalRun | None:
        return self.runs.get((owner_scope, run_id))


class _Runner:
    def __init__(self) -> None:
        self.store = _RunStore()
        self.calls: list[dict[str, Any]] = []

    async def run(self, record: SandboxRecord, cases: list[Any], **kwargs: Any) -> EvalRun:
        self.calls.append({"record": record, "cases": cases, **kwargs})
        run = EvalRun(
            id="eval-1",
            sandbox_id=record.id,
            agent=record.spec.agent.name,
            owner_scope=kwargs["owner_scope"],
            dataset_ref=kwargs["dataset_ref"],
            suite_ref=kwargs["suite_ref"],
            results=[
                CaseResult(
                    name="refund",
                    passed=True,
                    invocation_id="inv-1",
                    execution_backend="kubernetes_job",
                    scores={
                        "task_completion": {
                            "name": "task_completion",
                            "score": 1.0,
                            "passed": True,
                            "unit": "ratio",
                            "detail": {},
                        }
                    },
                )
            ],
        )
        self.store.runs[(kwargs["owner_scope"], run.id)] = run
        return run


class _Sandboxes:
    def __init__(self) -> None:
        now = datetime.now(UTC)
        self.record = SandboxRecord(
            id="sb-1",
            owner=ALICE.user_scope_id,
            spec=SandboxSpec(
                agent=AgentRef(name="support-agent", version="7"),
                model=ModelRef(provider="anthropic", model="claude-sonnet-4"),
                dataset=DatasetRef(ref="golden", version="3"),
            ),
            status=SandboxStatus.READY,
            created_at=now,
            expires_at=now + timedelta(hours=1),
        )
        self.created: list[SandboxSpec] = []

    async def get(self, sandbox_id: str, *, owner: str, is_admin: bool = False) -> SandboxRecord | None:
        if sandbox_id == self.record.id and owner == self.record.owner and not is_admin:
            return self.record
        return None

    async def create(self, spec: SandboxSpec, **_kwargs: Any) -> SandboxRecord:
        self.created.append(spec)
        self.record = self.record.model_copy(update={"spec": spec})
        return self.record


def _evaluation_run_app(principal: Principal, *, runner: _Runner, sandboxes: _Sandboxes) -> TestClient:
    app = _app(principal)
    app.state.sandbox_evals = runner
    app.state.sandbox_service = sandboxes
    return TestClient(app)


def _create_suite(client: TestClient) -> None:
    assert client.post("/api/evaluations/datasets", json=_dataset_body()).status_code == 201
    response = client.post(
        "/api/evaluations/suites",
        json={
            "name": "release-gate",
            "version": "2",
            "dataset": {"name": "golden", "version": "3"},
            "scorers": ["task_completion"],
        },
    )
    assert response.status_code == 201


def test_top_level_evaluation_reuses_an_owned_job_backed_sandbox_and_links_traces() -> None:
    runner = _Runner()
    sandboxes = _Sandboxes()
    client = _evaluation_run_app(ALICE, runner=runner, sandboxes=sandboxes)
    _create_suite(client)

    response = client.post(
        "/api/evaluations",
        json={"suite": {"name": "release-gate", "version": "2"}, "sandbox_id": "sb-1"},
    )

    assert response.status_code == 201, response.text
    assert response.json()["results"][0]["trace_url"] == "/api/traces/inv-1"
    assert response.json()["results"][0]["execution_backend"] == "kubernetes_job"
    assert runner.calls[0]["scorers"] == ["task_completion"]


def test_top_level_evaluation_can_provision_a_sandbox_pinned_to_the_suite_dataset() -> None:
    runner = _Runner()
    sandboxes = _Sandboxes()
    client = _evaluation_run_app(ALICE, runner=runner, sandboxes=sandboxes)
    _create_suite(client)

    response = client.post(
        "/api/evaluations",
        json={
            "suite": {"name": "release-gate", "version": "2"},
            "sandbox": {
                "agent": {"name": "support-agent", "version": "7"},
                "model": {"provider": "anthropic", "model": "claude-sonnet-4"},
            },
        },
    )

    assert response.status_code == 201, response.text
    assert sandboxes.created[0].dataset is not None
    assert sandboxes.created[0].dataset.ref == "golden"
    assert sandboxes.created[0].dataset.version == "3"


def test_top_level_evaluation_rejects_reuse_with_a_different_dataset_version() -> None:
    runner = _Runner()
    sandboxes = _Sandboxes()
    sandboxes.record = sandboxes.record.model_copy(
        update={"spec": sandboxes.record.spec.model_copy(update={"dataset": DatasetRef(ref="golden", version="2")})}
    )
    client = _evaluation_run_app(ALICE, runner=runner, sandboxes=sandboxes)
    _create_suite(client)

    response = client.post(
        "/api/evaluations",
        json={"suite": {"name": "release-gate", "version": "2"}, "sandbox_id": "sb-1"},
    )

    assert response.status_code == 422
    assert "exact dataset version" in response.text


def test_top_level_evaluation_get_is_scoped_to_the_authenticated_user() -> None:
    runner = _Runner()
    sandboxes = _Sandboxes()
    alice = _evaluation_run_app(ALICE, runner=runner, sandboxes=sandboxes)
    _create_suite(alice)
    assert (
        alice.post(
            "/api/evaluations",
            json={"suite": {"name": "release-gate", "version": "2"}, "sandbox_id": "sb-1"},
        ).status_code
        == 201
    )

    assert alice.get("/api/evaluations/eval-1").status_code == 200

    bob = _evaluation_run_app(BOB, runner=runner, sandboxes=sandboxes)
    assert bob.get("/api/evaluations/eval-1").status_code == 404
