from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from devai.identity import Principal
from devai.registry.client import Dataset, EvalSuite
from devai.registry.routes import _owner_id, router


def _headers(user: str) -> dict[str, str]:
    return {
        "x-forwarded-user": f"{user}@example.com",
        "x-forwarded-uid": user,
        "x-forwarded-tenant": "tenant-a",
    }


class _Registry:
    _namespace = "devai"

    def __init__(self) -> None:
        alice_owner = _owner_id(Principal(email="alice@example.com", uid="alice", tenant_id="tenant-a"))
        self.datasets = [
            Dataset(
                name="golden",
                description="Golden cases",
                version="3",
                case_count=1,
                raw={"labels": {"devai.tesserix.app/owner-id": alice_owner}, "visibility": "private"},
            )
        ]
        self.suites = [
            EvalSuite(
                name="release-gate",
                description="Release gate",
                version="2",
                dataset_ref={"ref": "golden", "version": "3"},
                scorers=["exact_match"],
                raw={"labels": {"devai.tesserix.app/owner-id": alice_owner}, "visibility": "private"},
            )
        ]
        self.published: dict[str, dict[str, Any]] = {}

    def list_skills(self) -> list[Any]:
        return []

    def list_prompts(self) -> list[Any]:
        return []

    def list_mcp_servers(self) -> list[Any]:
        return []

    def list_agents(self) -> list[Any]:
        return []

    def list_datasets(self) -> list[Dataset]:
        return self.datasets

    def list_eval_suites(self) -> list[EvalSuite]:
        return self.suites

    def get_artifact_envelope(self, plural: str, name: str) -> dict[str, Any] | None:
        return deepcopy(self.published.get(f"{plural}:{name}"))

    def publish_dataset(self, body: dict[str, Any]) -> dict[str, Any]:
        self.published[f"datasets:{body['metadata']['name']}"] = deepcopy(body)
        return {"name": body["metadata"]["name"]}

    def refresh(self) -> None:
        return None


def _client() -> tuple[TestClient, _Registry]:
    app = FastAPI()
    registry = _Registry()
    app.state.registry_client = registry
    app.state.config = SimpleNamespace(auth_bff_shared_secret="", trust_forwarded_without_secret=True)
    app.include_router(router)
    return TestClient(app), registry


def test_counts_and_lists_include_only_the_callers_private_eval_artifacts() -> None:
    client, _ = _client()

    alice_counts = client.get("/api/registry/counts", headers=_headers("alice"))
    alice_datasets = client.get("/api/registry/datasets", headers=_headers("alice"))
    bob_datasets = client.get("/api/registry/datasets", headers=_headers("bob"))
    alice_suites = client.get("/api/registry/eval-suites", headers=_headers("alice"))

    assert alice_counts.status_code == 200, alice_counts.text
    assert alice_counts.json()["datasets"] == 1
    assert alice_counts.json()["eval_suites"] == 1
    assert [item["name"] for item in alice_datasets.json()] == ["golden"]
    assert bob_datasets.json() == []
    assert alice_suites.json()[0]["dataset_ref"] == {"ref": "golden", "version": "3"}


def test_dashboard_can_publish_a_private_dataset_manifest_without_double_envelope() -> None:
    client, registry = _client()
    manifest = {
        "apiVersion": "registry.agentic.dev/v1alpha1",
        "kind": "Dataset",
        "metadata": {"name": "new-golden", "labels": {}},
        "spec": {"cases": [{"id": "refund", "input": "Refund order 4471"}]},
    }

    response = client.post("/api/registry/datasets", headers=_headers("alice"), json=manifest)

    assert response.status_code == 201, response.text
    stored = registry.published["datasets:new-golden"]
    assert stored["kind"] == "Dataset"
    assert stored["spec"] == manifest["spec"]
    assert "apiVersion" not in stored["spec"]
    assert stored["metadata"]["visibility"] == "private"
