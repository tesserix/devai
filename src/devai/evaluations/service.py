from __future__ import annotations

import asyncio
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
from devai.sandbox.evals import EvalCase


class EvaluationError(RuntimeError):
    pass


class EvaluationConflict(EvaluationError):
    pass


class EvaluationNotFound(EvaluationError):
    pass


class EvaluationInvalid(EvaluationError):
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


class EvaluationRegistry(Protocol):
    def get_artifact_envelope(self, plural: str, name: str) -> dict[str, Any] | None: ...


class EvaluationService:
    def __init__(
        self,
        *,
        database: EvaluationDatabase,
        object_store: ObjectStoreAdapter,
        registry: EvaluationRegistry | None = None,
    ) -> None:
        self._database = database
        self._object_store = object_store
        self._registry = registry

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
        from devai.evaluations.scorers import known

        unknown = sorted(set(request.scorers) - set(known()))
        if unknown:
            raise EvaluationInvalid(f"unknown scorer(s): {', '.join(unknown)}")
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
            thresholds=self._suite_configuration(request),
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
        try:
            dataset = await self.get_dataset(principal, ref.name, ref.version)
        except EvaluationNotFound:
            cases = await self._resolve_builtin_dataset(ref)
            return ResolvedEvaluation(cases=cases, dataset=ref)
        return ResolvedEvaluation(cases=[case.as_eval_case() for case in dataset.cases], dataset=ref)

    async def resolve_suite(self, principal: Principal, ref: ArtifactVersionRef) -> ResolvedEvaluation:
        try:
            suite = await self.get_suite(principal, ref.name, ref.version)
        except EvaluationNotFound:
            return await self._resolve_builtin_suite(ref)
        dataset = await self.get_dataset(principal, suite.dataset.name, suite.dataset.version)
        return ResolvedEvaluation(
            cases=[case.as_eval_case() for case in dataset.cases],
            dataset=suite.dataset,
            suite=ref,
            scorers=suite.scorers,
            thresholds=suite.thresholds,
            judge=suite.judge,
        )

    async def _resolve_builtin_suite(self, ref: ArtifactVersionRef) -> ResolvedEvaluation:
        spec = await self._builtin_spec("eval-suites", "EvalSuite", ref)
        dataset_value = spec.get("datasetRef")
        if not isinstance(dataset_value, dict):
            raise EvaluationError(f"built-in eval suite {ref.name}@{ref.version} has no dataset reference")
        try:
            dataset_ref = ArtifactVersionRef.model_validate(
                {"name": dataset_value.get("ref"), "version": dataset_value.get("version")}
            )
            scorers = [str(name) for name in spec.get("scorers") or []]
            thresholds = EvalThresholds.model_validate(spec.get("thresholds") or {})
        except (TypeError, ValueError) as error:
            raise EvaluationError(f"built-in eval suite {ref.name}@{ref.version} is invalid") from error
        from devai.evaluations.scorers import known

        if not scorers or len(scorers) != len(set(scorers)) or set(scorers) - set(known()):
            raise EvaluationError(f"built-in eval suite {ref.name}@{ref.version} has invalid scorers")
        cases = await self._resolve_builtin_dataset(dataset_ref)
        return ResolvedEvaluation(
            cases=cases,
            dataset=dataset_ref,
            suite=ref,
            scorers=scorers,
            thresholds=thresholds,
        )

    async def _resolve_builtin_dataset(self, ref: ArtifactVersionRef) -> list[EvalCase]:
        spec = await self._builtin_spec("datasets", "Dataset", ref)
        values = spec.get("cases")
        if not isinstance(values, list) or not 1 <= len(values) <= 50:
            raise EvaluationError(f"built-in dataset {ref.name}@{ref.version} has invalid cases")
        try:
            cases = [EvalCase.model_validate(value) for value in values]
        except (TypeError, ValueError) as error:
            raise EvaluationError(f"built-in dataset {ref.name}@{ref.version} has invalid cases") from error
        names = [case.name for case in cases]
        if len(names) != len(set(names)):
            raise EvaluationError(f"built-in dataset {ref.name}@{ref.version} has duplicate cases")
        return cases

    async def _builtin_spec(self, plural: str, kind: str, ref: ArtifactVersionRef) -> dict[str, Any]:
        if self._registry is None:
            raise EvaluationNotFound(f"{kind.lower()} {ref.name}@{ref.version} not found")
        try:
            envelope = await asyncio.to_thread(self._registry.get_artifact_envelope, plural, ref.name)
        except Exception as error:  # noqa: BLE001 — registry dependency errors fail closed
            raise EvaluationError("built-in evaluation registry unavailable") from error
        if not isinstance(envelope, dict) or envelope.get("kind") != kind:
            raise EvaluationNotFound(f"{kind.lower()} {ref.name}@{ref.version} not found")
        metadata = envelope.get("metadata")
        spec = envelope.get("spec")
        if not isinstance(metadata, dict) or not isinstance(spec, dict):
            raise EvaluationNotFound(f"{kind.lower()} {ref.name}@{ref.version} not found")
        labels = metadata.get("labels")
        source = labels.get("devai.io/source") if isinstance(labels, dict) else None
        version = str(metadata.get("tag") or spec.get("version") or "")
        if (
            metadata.get("name") != ref.name
            or metadata.get("namespace") != "devai"
            or metadata.get("visibility") != "public"
            or source != "devai"
            or version != ref.version
        ):
            raise EvaluationNotFound(f"{kind.lower()} {ref.name}@{ref.version} not found")
        return {str(key): value for key, value in spec.items()}

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
        configuration = dict(row.get("thresholds") or {})
        judge = configuration.pop("_judge", None)
        return EvalSuite(
            name=row["name"],
            version=row["version"],
            description=row.get("description") or "",
            dataset=ArtifactVersionRef(name=row["dataset_name"], version=row["dataset_version"]),
            scorers=list(row.get("scorers") or []),
            thresholds=EvalThresholds.model_validate(configuration),
            judge=judge,
            owner_scope=row["owner_scope"],
            created_at=str(row["created_at"]),
        )

    @staticmethod
    def _suite_configuration(request: EvalSuiteCreate) -> dict[str, Any]:
        configuration = request.thresholds.model_dump(mode="json", exclude_none=True)
        if request.judge is not None:
            configuration["_judge"] = request.judge.model_dump(mode="json")
        return configuration


__all__ = [
    "EvaluationConflict",
    "EvaluationError",
    "EvaluationInvalid",
    "EvaluationNotFound",
    "EvaluationService",
]
