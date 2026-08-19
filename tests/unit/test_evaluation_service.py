from __future__ import annotations

from typing import Any

import pytest

from devai.adapters.object_store.noop import NoopObjectStoreAdapter
from devai.evaluations.models import (
    ArtifactVersionRef,
    DatasetCase,
    DatasetCreate,
    EvalSuiteCreate,
    EvalThresholds,
)
from devai.evaluations.service import EvaluationConflict, EvaluationNotFound, EvaluationService
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

    async def get_eval_dataset_version(
        self,
        owner_scope: str,
        name: str,
        version: str,
    ) -> dict[str, Any] | None:
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

    async def get_eval_suite(
        self,
        owner_scope: str,
        name: str,
        version: str,
    ) -> dict[str, Any] | None:
        return self.suites.get((owner_scope, name, version))


def _principal(tenant: str = "tenant-a", uid: str = "alice") -> Principal:
    return Principal(email=f"{uid}@example.com", uid=uid, tenant_id=tenant, auth_provider="auth-bff")


def _dataset(version: str = "3") -> DatasetCreate:
    return DatasetCreate(
        name="remediation-golden",
        version=version,
        description="Pinned remediation behavior",
        cases=[
            DatasetCase(
                id="refund-happy-path",
                input="Refund order 4471",
                expected_output="refund complete",
                expected_tools=["customer_search", "eligibility_check", "refund"],
                forbidden_tools=["delete_resource"],
                context={"order_id": 4471},
                tags=["happy-path"],
            )
        ],
    )


async def test_dataset_versions_are_immutable_content_addressed_blobs() -> None:
    database = _Database()
    objects = NoopObjectStoreAdapter()
    service = EvaluationService(database=database, object_store=objects)

    created = await service.create_dataset(_principal(), _dataset())

    assert created.owner_scope == "tenant-a:alice"
    assert created.content_hash
    assert created.blob_key == f"evaluations/datasets/sha256/{created.content_hash}.json"
    assert await objects.get(created.blob_key)
    with pytest.raises(EvaluationConflict, match="already exists"):
        await service.create_dataset(_principal(), _dataset())


async def test_dataset_reads_are_scoped_to_the_exact_authenticated_user() -> None:
    service = EvaluationService(database=_Database(), object_store=NoopObjectStoreAdapter())
    await service.create_dataset(_principal(), _dataset())

    with pytest.raises(EvaluationNotFound):
        await service.get_dataset(_principal(tenant="tenant-b"), "remediation-golden", "3")
    with pytest.raises(EvaluationNotFound):
        await service.get_dataset(_principal(uid="bob"), "remediation-golden", "3")


async def test_suite_pins_an_existing_dataset_version_and_structured_thresholds() -> None:
    service = EvaluationService(database=_Database(), object_store=NoopObjectStoreAdapter())
    await service.create_dataset(_principal(), _dataset())

    suite = await service.create_suite(
        _principal(),
        EvalSuiteCreate(
            name="release-gate",
            version="2",
            dataset=ArtifactVersionRef(name="remediation-golden", version="3"),
            scorers=["exact_match", "tool_trajectory", "latency", "cost"],
            thresholds=EvalThresholds(
                success=0.95,
                safety=1.0,
                p95_latency_s=3,
                cost_per_run_usd=0.05,
            ),
        ),
    )

    assert suite.dataset == ArtifactVersionRef(name="remediation-golden", version="3")
    assert suite.thresholds.success == 0.95
    with pytest.raises(EvaluationNotFound):
        await service.create_suite(
            _principal(uid="bob"),
            EvalSuiteCreate(
                name="foreign-gate",
                version="1",
                dataset=ArtifactVersionRef(name="remediation-golden", version="3"),
                scorers=["exact_match"],
            ),
        )


async def test_resolving_a_suite_records_and_loads_the_exact_dataset_version() -> None:
    service = EvaluationService(database=_Database(), object_store=NoopObjectStoreAdapter())
    await service.create_dataset(_principal(), _dataset(version="3"))
    await service.create_dataset(_principal(), _dataset(version="4"))
    await service.create_suite(
        _principal(),
        EvalSuiteCreate(
            name="release-gate",
            version="2",
            dataset=ArtifactVersionRef(name="remediation-golden", version="3"),
            scorers=["tool_trajectory"],
        ),
    )

    resolved = await service.resolve_suite(_principal(), ArtifactVersionRef(name="release-gate", version="2"))

    assert resolved.dataset == ArtifactVersionRef(name="remediation-golden", version="3")
    assert resolved.suite == ArtifactVersionRef(name="release-gate", version="2")
    assert resolved.cases[0].name == "refund-happy-path"
    assert resolved.cases[0].expect.tools_called == ["customer_search", "eligibility_check", "refund"]
    assert resolved.cases[0].expect.tools_not_called == ["delete_resource"]
