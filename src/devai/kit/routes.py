"""The runtime versions an agent may be pinned to — mounted at ``/api/adk``."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request

if TYPE_CHECKING:
    from devai.kit.versions import AdkVersionCatalogue

router = APIRouter(prefix="/api/adk", tags=["adk"])


def _catalogue(request: Request) -> AdkVersionCatalogue:
    cat: AdkVersionCatalogue | None = getattr(request.app.state, "adk_catalogue", None)
    if cat is None:
        raise HTTPException(status_code=503, detail="adk version catalogue unavailable")
    return cat


@router.get("/versions")
async def list_versions(request: Request) -> dict[str, object]:
    versions = await _catalogue(request).versions()
    return {"versions": versions, "default": versions[0]}
