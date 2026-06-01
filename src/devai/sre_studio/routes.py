"""FastAPI routes for SRE Studio — mounted at ``/api/sre-studio/*``.

Author → dry-run → publish for custom SRE blueprints and agents. Lives in
the DevAI (ALM) app; the SRE runtime consumes whatever gets published.
503s cleanly when the service isn't wired; 422 on invalid YAML.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from devai.identity import extract_principal
from devai.sre_studio.service import SREStudioError

if TYPE_CHECKING:
    from devai.sre_studio.service import SREStudioService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sre-studio", tags=["sre-studio"])


def _service(request: Request) -> SREStudioService:
    svc = getattr(request.app.state, "sre_studio_service", None)
    if svc is None:
        raise HTTPException(status_code=503, detail="SRE Studio service unavailable")
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


class CreateDraftBody(BaseModel):
    kind: str = Field(..., pattern="^(blueprint|agent)$")
    yaml: str = Field(..., min_length=1, max_length=100000)
    description: str = Field("", max_length=2000)


class UpdateDraftBody(BaseModel):
    yaml: str | None = Field(None, max_length=100000)
    name: str | None = Field(None, max_length=200)
    description: str | None = Field(None, max_length=2000)


class DryRunBody(BaseModel):
    cluster_id: str = Field("default", max_length=200)


@router.get("/drafts")
async def list_drafts(request: Request, status: str | None = None) -> list[dict[str, Any]]:
    return await _service(request).list_drafts(status=status)


@router.get("/drafts/{draft_id}")
async def get_draft(request: Request, draft_id: str) -> dict[str, Any]:
    row = await _service(request).get_draft(draft_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"draft {draft_id!r} not found")
    return row


@router.post("/drafts", status_code=201)
async def create_draft(request: Request, body: CreateDraftBody) -> dict[str, Any]:
    try:
        return await _service(request).create_draft(
            body.kind, body.yaml, created_by=await _principal(request), description=body.description
        )
    except SREStudioError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.patch("/drafts/{draft_id}")
async def update_draft(request: Request, draft_id: str, body: UpdateDraftBody) -> dict[str, Any]:
    try:
        row = await _service(request).update_draft(
            draft_id, yaml_text=body.yaml, name=body.name, description=body.description
        )
    except SREStudioError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    if row is None:
        raise HTTPException(status_code=404, detail=f"draft {draft_id!r} not found")
    return row


@router.delete("/drafts/{draft_id}")
async def delete_draft(request: Request, draft_id: str) -> dict[str, Any]:
    if not await _service(request).delete_draft(draft_id):
        raise HTTPException(status_code=404, detail=f"draft {draft_id!r} not found")
    return {"deleted": draft_id}


@router.post("/drafts/{draft_id}/dry-run")
async def dry_run(request: Request, draft_id: str, body: DryRunBody) -> dict[str, Any]:
    try:
        return await _service(request).dry_run(draft_id, cluster_id=body.cluster_id)
    except SREStudioError as e:
        # 404 for "not found", 422 for "wrong kind / unavailable runtime".
        status = 404 if "not found" in str(e) else 422
        raise HTTPException(status_code=status, detail=str(e)) from e


@router.post("/drafts/{draft_id}/publish")
async def publish_draft(request: Request, draft_id: str) -> dict[str, Any]:
    try:
        return await _service(request).publish(draft_id, created_by=await _principal(request))
    except SREStudioError as e:
        status = 404 if "not found" in str(e) else 422
        raise HTTPException(status_code=status, detail=str(e)) from e


__all__ = ["router"]
