"""FastAPI routes for agent sandboxes — mounted at ``/api/sandboxes``.

Ownership mirrors the preview routes: a foreign sandbox reads as 404 rather than
403, and an anonymous caller (auth off) is pinned to a per-request owner so
anonymous sandboxes never cross-read one another.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import ValidationError

from devai.identity import Principal, extract_principal
from devai.sandbox.models import SandboxRecord, SandboxSpec
from devai.sandbox.service import SandboxError

if TYPE_CHECKING:
    from devai.sandbox.service import SandboxService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sandboxes", tags=["sandbox"])


def _service(request: Request) -> SandboxService:
    svc: SandboxService | None = getattr(request.app.state, "sandbox_service", None)
    if svc is None:
        raise HTTPException(status_code=503, detail="sandbox service unavailable")
    return svc


def _require_auth(request: Request) -> bool:
    config = getattr(request.app.state, "config", None)
    return bool(getattr(config, "require_auth", False))


async def _resolve_principal(request: Request) -> Principal | None:
    try:
        return await extract_principal(request)
    except Exception:  # noqa: BLE001 — identity lookup failure must not 500
        return None


def _owner_handle(principal: Principal | None) -> str | None:
    if principal is None:
        return None
    for attr in ("email", "display_name", "uid"):
        val = getattr(principal, attr, None)
        if val:
            return str(val)
    return None


async def _write_scope(request: Request) -> tuple[str, bool]:
    principal = await _resolve_principal(request)
    owner = _owner_handle(principal)
    if owner is None:
        if _require_auth(request):
            raise HTTPException(status_code=401, detail="authentication required")
        owner = f"anon:{uuid.uuid4().hex[:12]}"
    return owner, _is_admin(principal)


async def _read_scope(request: Request) -> tuple[str, bool]:
    principal = await _resolve_principal(request)
    owner = _owner_handle(principal)
    if owner is None and _require_auth(request):
        raise HTTPException(status_code=401, detail="authentication required")
    return owner or "", _is_admin(principal)


def _is_admin(principal: Principal | None) -> bool:
    return bool(principal and "admin" in (principal.roles or []))


def _view(record: SandboxRecord) -> dict[str, Any]:
    return record.model_dump(mode="json")


@router.post("", status_code=201)
async def create_sandbox(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    svc = _service(request)
    owner, _ = await _write_scope(request)
    try:
        spec = SandboxSpec.model_validate(body)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=e.errors(include_url=False)) from e
    try:
        return _view(await svc.create(spec, owner=owner))
    except SandboxError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.get("")
async def list_sandboxes(request: Request, mine: bool = True) -> list[dict[str, Any]]:
    owner, is_admin = await _read_scope(request)
    records = await _service(request).list(owner=owner, is_admin=is_admin and not mine)
    return [_view(r) for r in records]


@router.get("/{sandbox_id}")
async def get_sandbox(request: Request, sandbox_id: str) -> dict[str, Any]:
    owner, is_admin = await _read_scope(request)
    record = await _service(request).get(sandbox_id, owner=owner, is_admin=is_admin)
    if record is None:
        raise HTTPException(status_code=404, detail=f"sandbox {sandbox_id!r} not found")
    return _view(record)


@router.delete("/{sandbox_id}")
async def destroy_sandbox(request: Request, sandbox_id: str) -> dict[str, Any]:
    owner, is_admin = await _read_scope(request)
    try:
        await _service(request).destroy(sandbox_id, owner=owner, is_admin=is_admin)
    except SandboxError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return {"destroyed": sandbox_id}


__all__ = ["router"]
