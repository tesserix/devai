"""FastAPI routes for authoring agents and blueprints.

Mounted at ``/api/authoring/*``. Create endpoints validate through the
strict loaders and return 422 on bad input; everything 503s cleanly when
the service isn't wired.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from devai.authoring.service import AuthoringError
from devai.identity import extract_principal

if TYPE_CHECKING:
    from devai.authoring.service import AuthoringService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/authoring", tags=["authoring"])


def _service(request: Request) -> AuthoringService:
    svc = getattr(request.app.state, "authoring_service", None)
    if svc is None:
        raise HTTPException(status_code=503, detail="authoring service unavailable")
    return svc


async def _principal(request: Request) -> str:
    try:
        p = await extract_principal(request)
    except Exception:  # noqa: BLE001
        p = None
    if p is not None:
        for attr in ("email", "display_name", "uid"):
            val = getattr(p, attr, None)
            if val:
                return str(val)
    return "operator"


# ── Agents (specializations) ──────────────────────────────────────────


class HandoverFieldSpec(BaseModel):
    type: str = "string"
    required: bool = True
    description: str = ""


class CreateAgentBody(BaseModel):
    # Either pass raw `yaml`, or the structured fields below.
    yaml: str = ""
    name: str = Field("", max_length=100)
    display_name: str = Field("", max_length=200)
    description: str = Field("", max_length=2000)
    category: str = "specialist"
    llm_provider: str = "auto"
    llm_model: str = ""
    temperature: float | None = None
    system_prompt: str = Field("", max_length=20000)
    allowed_tools: list[str] = Field(default_factory=list, max_length=100)
    handover_schema: dict[str, HandoverFieldSpec] = Field(default_factory=dict)
    risk_level: str = ""
    output_key: str = ""


@router.get("/specializations")
async def list_specializations(request: Request) -> list[dict[str, Any]]:
    return await _service(request).list_specializations()


@router.get("/specializations/{name}")
async def get_specialization(request: Request, name: str) -> dict[str, Any]:
    row = await _service(request).get_specialization(name)
    if row is None:
        raise HTTPException(status_code=404, detail=f"agent {name!r} not found")
    return row


@router.post("/specializations", status_code=201)
async def create_specialization(request: Request, body: CreateAgentBody) -> dict[str, Any]:
    svc = _service(request)
    created_by = await _principal(request)
    try:
        if body.yaml.strip():
            return await svc.create_specialization(body.yaml, created_by=created_by)
        if not body.name:
            raise HTTPException(status_code=422, detail="name (or yaml) is required")
        fields = body.model_dump()
        fields["handover_schema"] = {
            k: v.model_dump() if isinstance(v, HandoverFieldSpec) else v
            for k, v in (body.handover_schema or {}).items()
        }
        return await svc.create_specialization_from_fields(fields, created_by=created_by)
    except AuthoringError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.delete("/specializations/{name}")
async def delete_specialization(request: Request, name: str) -> dict[str, Any]:
    deleted = await _service(request).delete_specialization(name)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"agent {name!r} not found")
    return {"deleted": name}


# ── Blueprints (workflows) ────────────────────────────────────────────


class CreateBlueprintBody(BaseModel):
    yaml: str = Field(..., min_length=1, max_length=100000)


@router.get("/blueprints")
async def list_blueprints(request: Request) -> list[dict[str, Any]]:
    return await _service(request).list_blueprints()


@router.get("/blueprints/{name}")
async def get_blueprint(request: Request, name: str) -> dict[str, Any]:
    row = await _service(request).get_blueprint(name)
    if row is None:
        raise HTTPException(status_code=404, detail=f"blueprint {name!r} not found")
    return row


@router.post("/blueprints", status_code=201)
async def create_blueprint(request: Request, body: CreateBlueprintBody) -> dict[str, Any]:
    svc = _service(request)
    try:
        return await svc.create_blueprint(body.yaml, created_by=await _principal(request))
    except AuthoringError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.delete("/blueprints/{name}")
async def delete_blueprint(request: Request, name: str) -> dict[str, Any]:
    deleted = await _service(request).delete_blueprint(name)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"blueprint {name!r} not found")
    return {"deleted": name}


__all__ = ["router"]
