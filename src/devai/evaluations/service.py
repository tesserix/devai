from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Protocol

from devai.adapters.object_store.base import ObjectStoreAdapter
from devai.evaluations.models import (
    ArtifactVersionRef,
    ComparisonAxis,
    ComparisonAxisName,
    ComparisonCase,
    ComparisonCreate,
    ComparisonMetric,
    DatasetCase,
    DatasetCreate,
    DatasetVersion,
    EvalSuite,
    EvalSuiteCreate,
    EvalThresholds,
    EvaluationComparison,
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

    async def get_eval_run_by_id(self, owner_scope: str, run_id: str) -> dict[str, Any] | None: ...

    async def create_eval_comparison(self, **values: Any) -> dict[str, Any]: ...

    async def get_eval_comparison(self, owner_scope: str, comparison_id: str) -> dict[str, Any] | None: ...


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

    async def create_comparison(
        self,
        principal: Principal,
        request: ComparisonCreate,
    ) -> EvaluationComparison:
        owner_scope = self._owner_scope(principal)
        baseline = await self._database.get_eval_run_by_id(owner_scope, request.baseline_run_id)
        candidate = await self._database.get_eval_run_by_id(owner_scope, request.candidate_run_id)
        if baseline is None or candidate is None:
            raise EvaluationNotFound("evaluation run not found")
        baseline_dataset = baseline.get("dataset")
        candidate_dataset = candidate.get("dataset")
        if not isinstance(baseline_dataset, dict) or baseline_dataset != candidate_dataset:
            raise EvaluationInvalid("evaluation runs must use the same dataset version")

        comparison_id = self._comparison_id(owner_scope, request)
        result = self._build_comparison(
            comparison_id,
            baseline,
            candidate,
            request.axes,
            created_at=datetime.now(UTC).isoformat(),
        )
        row = await self._database.create_eval_comparison(
            id=comparison_id,
            owner_scope=owner_scope,
            tenant_id=principal.tenant_id,
            user_id=principal.uid or principal.email,
            baseline_run_id=request.baseline_run_id,
            candidate_run_id=request.candidate_run_id,
            axes=list(request.axes),
            result=result.model_dump(mode="json"),
        )
        stored = dict(row.get("result") or result.model_dump(mode="json"))
        stored["created_at"] = str(row.get("created_at") or stored["created_at"])
        return EvaluationComparison.model_validate(stored)

    async def get_comparison(self, principal: Principal, comparison_id: str) -> EvaluationComparison:
        row = await self._database.get_eval_comparison(self._owner_scope(principal), comparison_id)
        if row is None:
            raise EvaluationNotFound(f"comparison {comparison_id} not found")
        body = dict(row.get("result") or {})
        body["created_at"] = str(row.get("created_at") or body.get("created_at") or "")
        return EvaluationComparison.model_validate(body)

    @staticmethod
    def _comparison_id(owner_scope: str, request: ComparisonCreate) -> str:
        canonical = json.dumps(
            [owner_scope, request.baseline_run_id, request.candidate_run_id, list(request.axes)],
            separators=(",", ":"),
        )
        return f"cmp-{hashlib.sha256(canonical.encode()).hexdigest()[:16]}"

    @classmethod
    def _build_comparison(
        cls,
        comparison_id: str,
        baseline: dict[str, Any],
        candidate: dict[str, Any],
        axis_names: list[ComparisonAxisName],
        *,
        created_at: str,
    ) -> EvaluationComparison:
        metrics = cls._comparison_metrics(baseline.get("summary") or {}, candidate.get("summary") or {})
        changed_cases = cls._changed_cases(baseline.get("results") or [], candidate.get("results") or [])
        regressions = [case for case in changed_cases if case.baseline_passed and not case.candidate_passed]
        newly_passing = [case for case in changed_cases if not case.baseline_passed and case.candidate_passed]
        axes = cls._comparison_axes(
            baseline.get("configuration") or {},
            candidate.get("configuration") or {},
            axis_names,
        )
        sample_size = min(len(baseline.get("results") or []), len(candidate.get("results") or []))
        caveat = (
            "Small sample: treat deltas as directional; statistical significance is not established."
            if sample_size < 30
            else "No significance test is inferred from one paired run; review repeated-run variance before promotion."
        )
        return EvaluationComparison(
            id=comparison_id,
            baseline_run_id=str(baseline["id"]),
            candidate_run_id=str(candidate["id"]),
            dataset={str(key): str(value) for key, value in baseline["dataset"].items()},
            metrics=metrics,
            axes=axes,
            changed_cases=changed_cases,
            regressions=regressions,
            newly_passing=newly_passing,
            sample_size=sample_size,
            caveat=caveat,
            summary=cls._comparison_summary(metrics, len(regressions)),
            created_at=created_at,
        )

    @staticmethod
    def _comparison_metrics(
        baseline: dict[str, Any],
        candidate: dict[str, Any],
    ) -> dict[str, ComparisonMetric]:
        baseline_values = EvaluationService._metric_values(baseline)
        candidate_values = EvaluationService._metric_values(candidate)
        metrics: dict[str, ComparisonMetric] = {}
        for name in sorted(set(baseline_values) | set(candidate_values)):
            baseline_value = baseline_values.get(name, 0.0)
            candidate_value = candidate_values.get(name, 0.0)
            delta = round(candidate_value - baseline_value, 6)
            percent_delta = None
            if baseline_value != 0:
                percent_delta = round(delta / abs(baseline_value) * 100, 4)
            metrics[name] = ComparisonMetric(
                baseline=baseline_value,
                candidate=candidate_value,
                delta=delta,
                percent_delta=percent_delta,
            )
        return metrics

    @staticmethod
    def _metric_values(summary: dict[str, Any]) -> dict[str, float]:
        values = {
            "success": float(summary.get("pass_rate") or 0.0),
            "p95_latency_ms": float(summary.get("p95_latency_ms") or 0.0),
            "cost_usd": float(summary.get("cost_usd") or 0.0),
            "total_tokens": float(summary.get("total_tokens") or 0.0),
        }
        dimensions = summary.get("dimensions") or {}
        if isinstance(dimensions, dict):
            for name, metric in dimensions.items():
                if isinstance(metric, dict):
                    values[str(name)] = float(metric.get("average", metric.get("pass_rate", 0.0)) or 0.0)
        return values

    @staticmethod
    def _changed_cases(
        baseline_results: list[dict[str, Any]],
        candidate_results: list[dict[str, Any]],
    ) -> list[ComparisonCase]:
        candidate_by_id = {str(result.get("name") or ""): result for result in candidate_results}
        changed: list[ComparisonCase] = []
        for baseline in baseline_results:
            case_id = str(baseline.get("name") or "")
            candidate = candidate_by_id.get(case_id)
            if candidate is None or bool(baseline.get("passed")) == bool(candidate.get("passed")):
                continue
            changed.append(
                ComparisonCase(
                    case_id=case_id,
                    baseline_passed=bool(baseline.get("passed")),
                    candidate_passed=bool(candidate.get("passed")),
                    baseline_trace_url=EvaluationService._trace_url(baseline),
                    candidate_trace_url=EvaluationService._trace_url(candidate),
                )
            )
        return changed

    @staticmethod
    def _trace_url(result: dict[str, Any]) -> str | None:
        trace_url = result.get("trace_url")
        if trace_url:
            return str(trace_url)
        invocation_id = result.get("invocation_id")
        return f"/api/traces/{invocation_id}" if invocation_id else None

    @staticmethod
    def _comparison_axes(
        baseline: dict[str, Any],
        candidate: dict[str, Any],
        names: list[ComparisonAxisName],
    ) -> dict[ComparisonAxisName, ComparisonAxis]:
        paths: dict[ComparisonAxisName, tuple[str, ...]] = {
            "prompt_version": ("prompt", "version"),
            "model": ("model",),
            "agent_version": ("agent", "version"),
            "tool_config": ("tools",),
        }

        def value(configuration: dict[str, Any], path: tuple[str, ...]) -> Any:
            current: Any = configuration
            for segment in path:
                if not isinstance(current, dict):
                    return None
                current = current.get(segment)
            return current

        axes: dict[ComparisonAxisName, ComparisonAxis] = {}
        for name in names:
            baseline_value = value(baseline, paths[name])
            candidate_value = value(candidate, paths[name])
            axes[name] = ComparisonAxis(
                baseline=baseline_value,
                candidate=candidate_value,
                changed=baseline_value != candidate_value,
            )
        return axes

    @staticmethod
    def _comparison_summary(metrics: dict[str, ComparisonMetric], regression_count: int) -> str:
        success = metrics.get("success")
        quality = "Candidate has no measured success-rate change"
        if success and success.delta > 0:
            quality = f"Candidate improves success by {success.delta * 100:.1f} percentage points"
        elif success and success.delta < 0:
            quality = f"Candidate reduces success by {abs(success.delta) * 100:.1f} percentage points"
        cost = metrics.get("cost_usd")
        latency = metrics.get("p95_latency_ms")
        cost_text = f"cost changes by {(cost.percent_delta or 0):+.1f}%" if cost else "cost is unavailable"
        latency_text = (
            f"P95 latency changes by {(latency.percent_delta or 0):+.1f}%" if latency else "P95 latency is unavailable"
        )
        regression_text = f"{regression_count} pass-to-fail regression(s) require review"
        return f"{quality}; {cost_text} and {latency_text}; {regression_text}."

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
