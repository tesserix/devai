"""Authenticated API for immutable Registry agent imports."""

from __future__ import annotations

import logging
from typing import Any, cast

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from devai.authz import require_principal
from devai.registry.imports import (
    AgentImportConflict,
    AgentImportInvalid,
    AgentImportNotFound,
    AgentImportService,
    AgentImportUnavailable,
    public_agent_import,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/registry/imports", tags=["registry-imports"])


class AgentImportCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    registry_ref: str = Field(min_length=1, max_length=1024)


def _service(request: Request) -> AgentImportService:
    service = getattr(request.app.state, "agent_import_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="agent import service unavailable")
    return cast("AgentImportService", service)


def _http_error(error: Exception) -> HTTPException:
    if isinstance(error, AgentImportInvalid):
        return HTTPException(status_code=422, detail=str(error))
    if isinstance(error, AgentImportConflict):
        return HTTPException(status_code=409, detail=str(error))
    if isinstance(error, AgentImportNotFound):
        return HTTPException(status_code=404, detail=str(error))
    if isinstance(error, AgentImportUnavailable):
        logger.warning("agent import dependency unavailable", exc_info=error)
        return HTTPException(status_code=503, detail=str(error))
    logger.exception("unexpected agent import failure")
    return HTTPException(status_code=503, detail="agent import service unavailable")


@router.post("", status_code=201)
async def create_agent_import(
    request: Request,
    body: AgentImportCreate,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=255),
) -> dict[str, Any]:
    principal = await require_principal(request)
    try:
        orchestrator = getattr(request.app.state, "agent_lifecycle_orchestrator", None)
        if orchestrator is not None:
            try:
                row = await orchestrator.import_agent(
                    principal,
                    project_id=body.project_id,
                    registry_ref=body.registry_ref,
                    idempotency_key=idempotency_key,
                )
            except Exception as error:  # noqa: BLE001 — Temporal fail-open is explicit configuration
                from devai.orchestration.agent_lifecycle_client import AgentLifecycleValidationError

                if isinstance(error, AgentLifecycleValidationError):
                    raise AgentImportInvalid(str(error)) from error
                if bool(getattr(request.app.state.config, "temporal_fail_closed", False)):
                    raise AgentImportUnavailable("durable Agent import workflow unavailable") from error
                logger.exception("durable Agent import failed; replaying through idempotent local service")
                row = await _service(request).create(
                    principal,
                    project_id=body.project_id,
                    registry_ref=body.registry_ref,
                    idempotency_key=idempotency_key,
                )
        else:
            row = await _service(request).create(
                principal,
                project_id=body.project_id,
                registry_ref=body.registry_ref,
                idempotency_key=idempotency_key,
            )
        return public_agent_import(row)
    except Exception as error:  # noqa: BLE001
        raise _http_error(error) from error


@router.get("")
async def list_agent_imports(
    request: Request,
    project_id: str = Query(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$"),
    limit: int = Query(default=100, ge=1, le=200),
) -> list[dict[str, Any]]:
    principal = await require_principal(request)
    try:
        rows = await _service(request).list(principal, project_id=project_id, limit=limit)
        return [public_agent_import(row) for row in rows]
    except Exception as error:  # noqa: BLE001
        raise _http_error(error) from error


@router.get("/{import_id}")
async def get_agent_import(request: Request, import_id: str) -> dict[str, Any]:
    principal = await require_principal(request)
    try:
        return public_agent_import(await _service(request).get(principal, import_id))
    except Exception as error:  # noqa: BLE001
        raise _http_error(error) from error


__all__ = ["router"]
