from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from devai.authz import require_principal
from devai.evaluations.models import DatasetCreate, DatasetVersion, EvalSuite, EvalSuiteCreate
from devai.evaluations.service import EvaluationConflict, EvaluationError, EvaluationNotFound, EvaluationService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/evaluations", tags=["evaluations"])


def _service(request: Request) -> EvaluationService:
    service: EvaluationService | None = getattr(request.app.state, "evaluation_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="evaluation storage unavailable")
    return service


def _dataset_view(dataset: DatasetVersion) -> dict[str, Any]:
    return dataset.model_dump(mode="json", exclude={"owner_scope", "blob_key"})


def _suite_view(suite: EvalSuite) -> dict[str, Any]:
    return suite.model_dump(mode="json", exclude={"owner_scope"})


def _translate_error(error: Exception) -> HTTPException:
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


__all__ = ["router"]
