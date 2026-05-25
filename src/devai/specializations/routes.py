"""FastAPI routes for specializations.

Mounted at `/api/specializations/*` by the webhook app. Reads from
`app.state.specialization_service`. Returns 503 when the service is
disabled / not started.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/api/specializations", tags=["specializations"])


def _service(request: Request):
    svc = getattr(request.app.state, "specialization_service", None)
    if svc is None:
        raise HTTPException(
            status_code=503,
            detail="specialization service not started — set DEVAI_SPECIALIZATIONS_ENABLED=true",
        )
    return svc


@router.get("")
async def list_specializations(request: Request, category: str = "") -> list[dict[str, Any]]:
    svc = _service(request)
    all_specs = svc.list_all()
    if category:
        all_specs = [s for s in all_specs if s.get("category") == category]
    return all_specs


@router.get("/by-category")
async def by_category(request: Request) -> dict[str, list[dict[str, Any]]]:
    return _service(request).by_category()


@router.get("/{name}")
async def get_specialization(request: Request, name: str) -> dict[str, Any]:
    svc = _service(request)
    spec = svc.get(name)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"specialization {name!r} not found")
    return spec


@router.get("/{name}/prompt")
async def get_specialization_prompt(request: Request, name: str) -> dict[str, str]:
    """Return the raw system prompt — useful for the dashboard's prompt-editor view."""
    svc = _service(request)
    spec = svc.get_full(name)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"specialization {name!r} not found")
    return {
        "name": spec.name,
        "system_prompt": spec.system_prompt,
        "user_prompt_template": spec.user_prompt_template,
    }


@router.post("/reload")
async def reload_specializations(request: Request) -> dict[str, int]:
    """Re-discover specs from disk. Returns the new total count."""
    svc = _service(request)
    new_count = await svc.reload()
    return {"loaded": new_count}


__all__ = ["router"]
