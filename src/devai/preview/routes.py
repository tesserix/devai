"""FastAPI routes for on-demand live previews — mounted at ``/api/preview/*``.

Start a hot-reloading preview for a repo, inspect it, list, and stop. The
returned ``preview_url`` is the unique forwarded host
(preview-<id>.tesserix.app) the dashboard iframes AND opens in a new tab —
the same URL for both, gated by devai-auth-bff. 503s cleanly until wired.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from devai.identity import extract_principal
from devai.preview.service import PreviewError

if TYPE_CHECKING:
    from devai.preview.service import PreviewService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/preview", tags=["preview"])


def _service(request: Request) -> PreviewService:
    svc = getattr(request.app.state, "preview_service", None)
    if svc is None:
        raise HTTPException(status_code=503, detail="preview service unavailable")
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


class StartPreviewBody(BaseModel):
    repo: str = Field(..., min_length=1, max_length=200)
    ref: str = Field("main", max_length=200)


@router.post("/start", status_code=201)
async def start_preview(request: Request, body: StartPreviewBody) -> dict[str, Any]:
    try:
        return await _service(request).start(body.repo, body.ref, owner=await _principal(request))
    except PreviewError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.get("")
async def list_previews(request: Request, mine: bool = False) -> list[dict[str, Any]]:
    owner = await _principal(request) if mine else None
    return await _service(request).list(owner=owner)


@router.get("/{session_id}")
async def get_preview(request: Request, session_id: str) -> dict[str, Any]:
    row = await _service(request).get(session_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"preview {session_id!r} not found")
    return row


@router.post("/{session_id}/verify")
async def verify_preview(request: Request, session_id: str, heal: bool = True) -> dict[str, Any]:
    """Diagnose (and by default self-heal) a preview's bring-up failures."""
    try:
        res = await _service(request).verify(session_id, heal=heal)
    except PreviewError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    if res is None:
        raise HTTPException(status_code=404, detail=f"preview {session_id!r} not found")
    return res


@router.post("/{session_id}/stop")
async def stop_preview(request: Request, session_id: str) -> dict[str, Any]:
    if not await _service(request).stop(session_id):
        raise HTTPException(status_code=404, detail=f"preview {session_id!r} not found")
    return {"stopped": session_id}


__all__ = ["router"]
