from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, cast

from fastapi import APIRouter, Header, HTTPException, Query, Request

from devai.authz import require_principal
from devai.evaluations.models import (
    ComparisonCreate,
    DatasetCreate,
    DatasetVersion,
    EvalSuite,
    EvalSuiteCreate,
    EvaluationRunCreate,
)
from devai.evaluations.service import (
    EvaluationConflict,
    EvaluationError,
    EvaluationInvalid,
    EvaluationNotFound,
    EvaluationService,
)
from devai.orchestration.agent_lifecycle_http import durable_result, require_idempotency_key
from devai.sandbox.models import DatasetRef, SandboxStatus
from devai.sandbox.service import SandboxError

if TYPE_CHECKING:
    from devai.sandbox.evals import EvalRunner
    from devai.sandbox.service import SandboxService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/evaluations", tags=["evaluations"])
comparison_router = APIRouter(prefix="/api/comparisons", tags=["evaluation-comparisons"])


def _service(request: Request) -> EvaluationService:
    service: EvaluationService | None = getattr(request.app.state, "evaluation_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="evaluation storage unavailable")
    return service


def _dataset_view(dataset: DatasetVersion) -> dict[str, Any]:
    return dataset.model_dump(mode="json", exclude={"owner_scope", "blob_key"})


def _suite_view(suite: EvalSuite) -> dict[str, Any]:
    return suite.model_dump(mode="json", exclude={"owner_scope"})


def _runner(request: Request) -> EvalRunner:
    runner = getattr(request.app.state, "sandbox_evals", None)
    if runner is None:
        raise HTTPException(status_code=503, detail="evaluation runner unavailable")
    return cast("EvalRunner", runner)


def _sandboxes(request: Request) -> SandboxService:
    service = getattr(request.app.state, "sandbox_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="sandbox service unavailable")
    return cast("SandboxService", service)


def _translate_error(error: Exception) -> HTTPException:
    if isinstance(error, EvaluationInvalid):
        return HTTPException(status_code=422, detail=str(error))
    if isinstance(error, EvaluationConflict):
        return HTTPException(status_code=409, detail=str(error))
    if isinstance(error, EvaluationNotFound):
        return HTTPException(status_code=404, detail=str(error))
    if isinstance(error, EvaluationError):
        logger.warning("evaluation storage unavailable", exc_info=error)
    else:
        logger.exception("evaluation dependency failed")
    return HTTPException(status_code=503, detail="evaluation storage unavailable")


@router.post("/datasets", status_code=201)
async def create_dataset(request: Request, body: DatasetCreate) -> dict[str, Any]:
    principal = await require_principal(request)
    try:
        return _dataset_view(await _service(request).create_dataset(principal, body))
    except Exception as error:  # noqa: BLE001 — request boundary maps dependency failures
        raise _translate_error(error) from error


@router.get("/datasets")
async def list_datasets(request: Request, limit: int = Query(100, ge=1, le=200)) -> list[dict[str, Any]]:
    principal = await require_principal(request)
    try:
        datasets = await _service(request).list_datasets(principal, limit=limit)
        return [_dataset_view(dataset) for dataset in datasets]
    except Exception as error:  # noqa: BLE001 — request boundary maps dependency failures
        raise _translate_error(error) from error


@router.get("/datasets/{name}/versions/{version}")
async def get_dataset(request: Request, name: str, version: str) -> dict[str, Any]:
    principal = await require_principal(request)
    try:
        return _dataset_view(await _service(request).get_dataset(principal, name, version))
    except Exception as error:  # noqa: BLE001 — request boundary maps dependency failures
        raise _translate_error(error) from error


@router.post("/suites", status_code=201)
async def create_suite(request: Request, body: EvalSuiteCreate) -> dict[str, Any]:
    principal = await require_principal(request)
    try:
        return _suite_view(await _service(request).create_suite(principal, body))
    except Exception as error:  # noqa: BLE001 — request boundary maps dependency failures
        raise _translate_error(error) from error


@router.get("/suites")
async def list_suites(request: Request, limit: int = Query(100, ge=1, le=200)) -> list[dict[str, Any]]:
    principal = await require_principal(request)
    try:
        suites = await _service(request).list_suites(principal, limit=limit)
        return [_suite_view(suite) for suite in suites]
    except Exception as error:  # noqa: BLE001 — request boundary maps dependency failures
        raise _translate_error(error) from error


@router.get("/suites/{name}/versions/{version}")
async def get_suite(request: Request, name: str, version: str) -> dict[str, Any]:
    principal = await require_principal(request)
    try:
        return _suite_view(await _service(request).get_suite(principal, name, version))
    except Exception as error:  # noqa: BLE001 — request boundary maps dependency failures
        raise _translate_error(error) from error


@router.post("", status_code=201)
async def run_evaluation(
    request: Request,
    body: EvaluationRunCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    principal = await require_principal(request)
    key = require_idempotency_key(request, idempotency_key)
    result = await durable_result(
        request,
        "evaluation",
        lambda orchestrator: orchestrator.evaluate(
            principal,
            body.model_dump(mode="json", exclude_none=True),
            request_id=key,
        ),
    )
    if result is not None:
        return result
    try:
        resolved = await _service(request).resolve_suite(principal, body.suite)
        owner_scope = principal.user_scope_id
        if not owner_scope:
            raise EvaluationInvalid("authenticated principal has no stable subject")
        sandboxes = _sandboxes(request)
        pinned_dataset = DatasetRef(ref=resolved.dataset.name, version=resolved.dataset.version)
        if body.sandbox_id is not None:
            record = await sandboxes.get(body.sandbox_id, owner=owner_scope, is_admin=False)
            if record is None:
                raise EvaluationNotFound(f"sandbox {body.sandbox_id} not found")
            if record.spec.dataset != pinned_dataset:
                raise EvaluationInvalid("sandbox does not pin the suite's exact dataset version")
        else:
            if body.sandbox is None:
                raise EvaluationInvalid("sandbox specification is required")
            if body.sandbox.dataset is not None and body.sandbox.dataset != pinned_dataset:
                raise EvaluationInvalid("sandbox dataset does not match the suite's pinned dataset version")
            spec = body.sandbox.model_copy(update={"dataset": pinned_dataset})
            record = await sandboxes.create(
                spec,
                owner=owner_scope,
                tenant_id=principal.tenant_id,
                user_id=principal.uid or principal.email,
            )
        if record.status != SandboxStatus.READY:
            raise EvaluationInvalid(f"sandbox {record.id} is not ready")
        run = await _runner(request).start(
            record,
            resolved.cases,
            triggered_by=owner_scope,
            owner_scope=owner_scope,
            tenant_id=principal.tenant_id,
            user_id=principal.uid or principal.email,
            dataset_ref=resolved.dataset.model_dump(mode="json"),
            suite_ref=body.suite.model_dump(mode="json"),
            scorers=resolved.scorers,
            principal=principal,
            judge_config=resolved.judge,
        )
        return run.to_dict()
    except HTTPException:
        raise
    except SandboxError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception as error:  # noqa: BLE001 — request boundary maps dependency failures
        raise _translate_error(error) from error


@router.get("/{run_id}")
async def get_evaluation_run(request: Request, run_id: str) -> dict[str, Any]:
    principal = await require_principal(request)
    owner_scope = principal.user_scope_id
    if not owner_scope:
        raise HTTPException(status_code=404, detail=f"evaluation {run_id} not found")
    try:
        run = await _runner(request).store.get_by_id(owner_scope, run_id)
    except Exception as error:  # noqa: BLE001 — durable reads fail closed
        logger.exception("durable evaluation read failed")
        raise HTTPException(status_code=503, detail="evaluation storage unavailable") from error
    if run is None:
        raise HTTPException(status_code=404, detail=f"evaluation {run_id} not found")
    return run.to_dict()


@comparison_router.post("", status_code=201)
async def create_comparison(
    request: Request,
    body: ComparisonCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    principal = await require_principal(request)
    key = require_idempotency_key(request, idempotency_key)
    result = await durable_result(
        request,
        "evaluation comparison",
        lambda orchestrator: orchestrator.compare(
            principal,
            body.model_dump(mode="json"),
            request_id=key,
        ),
    )
    if result is not None:
        return result
    try:
        comparison = await _service(request).create_comparison(principal, body)
        return comparison.model_dump(mode="json")
    except Exception as error:  # noqa: BLE001 — request boundary maps dependency failures
        raise _translate_error(error) from error


@comparison_router.get("/{comparison_id}")
async def get_comparison(request: Request, comparison_id: str) -> dict[str, Any]:
    principal = await require_principal(request)
    try:
        comparison = await _service(request).get_comparison(principal, comparison_id)
        return comparison.model_dump(mode="json")
    except Exception as error:  # noqa: BLE001 — request boundary maps dependency failures
        raise _translate_error(error) from error


__all__ = ["comparison_router", "router"]
