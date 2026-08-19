from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

import devai.evaluations.routes as evaluation_routes
from devai.adapters.object_store.noop import NoopObjectStoreAdapter
from devai.evaluations.service import EvaluationService
from devai.identity import Principal


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
            "scorers": ["exact_match", "tool_trajectory"],
            "thresholds": {"success": 0.95, "safety": 1.0},
        },
    )
    fetched = client.get("/api/evaluations/suites/release-gate/versions/2")

    assert created.status_code == 201
    assert fetched.status_code == 200
    assert fetched.json()["dataset"] == {"name": "golden", "version": "3"}


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
