from __future__ import annotations

import hashlib
import json
from typing import Any, Protocol

from devai.adapters.object_store.base import ObjectStoreAdapter
from devai.evaluations.models import (
    ArtifactVersionRef,
    DatasetCase,
    DatasetCreate,
    DatasetVersion,
    EvalSuite,
    EvalSuiteCreate,
    EvalThresholds,
    ResolvedEvaluation,
)
from devai.identity import Principal


class EvaluationError(RuntimeError):
    pass


class EvaluationConflict(EvaluationError):
    pass


class EvaluationNotFound(EvaluationError):
    pass


class EvaluationDatabase(Protocol):
    async def create_eval_dataset_version(self, **values: Any) -> dict[str, Any] | None: ...

    async def list_eval_datasets(self, owner_scope: str, *, limit: int) -> list[dict[str, Any]]: ...

    async def get_eval_dataset_version(
        self,
        owner_scope: str,
        name: str,
        version: str,
    ) -> dict[str, Any] | None: ...

    async def create_eval_suite(self, **values: Any) -> dict[str, Any] | None: ...

    async def list_eval_suites(self, owner_scope: str, *, limit: int) -> list[dict[str, Any]]: ...

    async def get_eval_suite(self, owner_scope: str, name: str, version: str) -> dict[str, Any] | None: ...


class EvaluationService:
    def __init__(self, *, database: EvaluationDatabase, object_store: ObjectStoreAdapter) -> None:
        self._database = database
        self._object_store = object_store

    async def create_dataset(self, principal: Principal, request: DatasetCreate) -> DatasetVersion:
        content = self._dataset_content(request)
        content_hash = hashlib.sha256(content).hexdigest()
        blob_key = f"evaluations/datasets/sha256/{content_hash}.json"
        if not await self._object_store.exists(blob_key):
            await self._object_store.put(blob_key, content, content_type="application/json")
        row = await self._database.create_eval_dataset_version(
            owner_scope=self._owner_scope(principal),
            tenant_id=principal.tenant_id,
            user_id=principal.uid or principal.email,
            name=request.name,
            version=request.version,
            description=request.description,
            case_count=len(request.cases),
            content_hash=content_hash,
            blob_key=blob_key,
        )
        if row is None:
            raise EvaluationConflict(f"dataset {request.name}@{request.version} already exists")
        return self._dataset_from_row(row, cases=request.cases)

    async def list_datasets(self, principal: Principal, *, limit: int = 100) -> list[DatasetVersion]:
        rows = await self._database.list_eval_datasets(self._owner_scope(principal), limit=limit)
        return [self._dataset_from_row(row) for row in rows]

    async def get_dataset(self, principal: Principal, name: str, version: str) -> DatasetVersion:
        row = await self._database.get_eval_dataset_version(self._owner_scope(principal), name, version)
        if row is None:
            raise EvaluationNotFound(f"dataset {name}@{version} not found")
        try:
            content = await self._object_store.get(str(row["blob_key"]))
            body = json.loads(content)
            cases = [DatasetCase.model_validate(case) for case in body["cases"]]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise EvaluationError(f"dataset {name}@{version} content unavailable") from error
        return self._dataset_from_row(row, cases=cases)

    async def create_suite(self, principal: Principal, request: EvalSuiteCreate) -> EvalSuite:
        await self.get_dataset(principal, request.dataset.name, request.dataset.version)
        row = await self._database.create_eval_suite(
            owner_scope=self._owner_scope(principal),
            tenant_id=principal.tenant_id,
            user_id=principal.uid or principal.email,
            name=request.name,
            version=request.version,
            description=request.description,
            dataset_name=request.dataset.name,
            dataset_version=request.dataset.version,
            scorers=list(request.scorers),
            thresholds=request.thresholds.model_dump(mode="json", exclude_none=True),
        )
        if row is None:
            raise EvaluationConflict(f"eval suite {request.name}@{request.version} already exists")
        return self._suite_from_row(row)

    async def list_suites(self, principal: Principal, *, limit: int = 100) -> list[EvalSuite]:
        rows = await self._database.list_eval_suites(self._owner_scope(principal), limit=limit)
        return [self._suite_from_row(row) for row in rows]

    async def get_suite(self, principal: Principal, name: str, version: str) -> EvalSuite:
        row = await self._database.get_eval_suite(self._owner_scope(principal), name, version)
        if row is None:
            raise EvaluationNotFound(f"eval suite {name}@{version} not found")
        return self._suite_from_row(row)

    async def resolve_dataset(self, principal: Principal, ref: ArtifactVersionRef) -> ResolvedEvaluation:
        dataset = await self.get_dataset(principal, ref.name, ref.version)
        return ResolvedEvaluation(cases=[case.as_eval_case() for case in dataset.cases], dataset=ref)

    async def resolve_suite(self, principal: Principal, ref: ArtifactVersionRef) -> ResolvedEvaluation:
        suite = await self.get_suite(principal, ref.name, ref.version)
        dataset = await self.get_dataset(principal, suite.dataset.name, suite.dataset.version)
        return ResolvedEvaluation(
            cases=[case.as_eval_case() for case in dataset.cases],
            dataset=suite.dataset,
            suite=ref,
        )

    @staticmethod
    def _owner_scope(principal: Principal) -> str:
        owner_scope = principal.user_scope_id
        if not owner_scope:
            raise EvaluationError("authenticated principal has no stable subject")
        return owner_scope

    @staticmethod
    def _dataset_content(request: DatasetCreate) -> bytes:
        body = {"cases": [case.model_dump(mode="json") for case in request.cases]}
        return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()

    @staticmethod
    def _dataset_from_row(row: dict[str, Any], *, cases: list[DatasetCase] | None = None) -> DatasetVersion:
        return DatasetVersion(
            name=row["name"],
            version=row["version"],
            description=row.get("description") or "",
            cases=cases or [],
            case_count=int(row["case_count"]),
            content_hash=row["content_hash"],
            blob_key=row["blob_key"],
            owner_scope=row["owner_scope"],
            created_at=str(row["created_at"]),
        )

    @staticmethod
    def _suite_from_row(row: dict[str, Any]) -> EvalSuite:
        return EvalSuite(
            name=row["name"],
            version=row["version"],
            description=row.get("description") or "",
            dataset=ArtifactVersionRef(name=row["dataset_name"], version=row["dataset_version"]),
            scorers=list(row.get("scorers") or []),
            thresholds=EvalThresholds.model_validate(row.get("thresholds") or {}),
            owner_scope=row["owner_scope"],
            created_at=str(row["created_at"]),
        )


__all__ = ["EvaluationConflict", "EvaluationError", "EvaluationNotFound", "EvaluationService"]
