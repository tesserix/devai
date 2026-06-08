"""FastAPI routes for on-demand live previews — mounted at ``/api/preview/*``.

Start a hot-reloading preview for a repo, inspect it, list, and stop. The
returned ``preview_url`` is the unique forwarded host
(preview-<id>.tesserix.app) the dashboard iframes AND opens in a new tab —
the same URL for both, gated by devai-auth-bff. 503s cleanly until wired.

Authorization: every mutating route (start/verify/stop) requires a resolvable
principal when ``DEVAI_REQUIRE_AUTH`` is on — anonymous callers get 401. When
auth is off (today's default) the gate is dormant and the routes work as
before, but each anonymous caller is still pinned to a per-request,
non-reusable owner so anonymous previews never cross-read one another. Reads
and ownership are enforced in :class:`PreviewService` (404 on a foreign row).
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from devai.identity import Principal, extract_principal
from devai.preview.service import PreviewError

if TYPE_CHECKING:
    from devai.preview.service import PreviewService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/preview", tags=["preview"])

# Validation at the API boundary (CODE-8): repo is ``owner/name`` and ref is a
# git ref. Both feed an in-pod ``git clone`` (job_spec build), so reject any
# shell/option metacharacter (a leading '-' would be parsed as a git flag) and
# anything outside a conservative allowlist.
_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._/-]+$")
_REF_RE = re.compile(r"^[A-Za-z0-9._/-]+$")


def _service(request: Request) -> PreviewService:
    svc = getattr(request.app.state, "preview_service", None)
    if svc is None:
        raise HTTPException(status_code=503, detail="preview service unavailable")
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
    """Stable owner key for a principal, or None when unresolved.

    Mirrors the audit-log handle order (email → display_name → uid). Returns
    None — never a shared literal like ``"operator"`` — so callers decide
    whether to 401 or mint a per-request anonymous owner (CODE-10).
    """
    if principal is None:
        return None
    for attr in ("email", "display_name", "uid"):
        val = getattr(principal, attr, None)
        if val:
            return str(val)
    return None


async def _principal_owner(request: Request) -> tuple[Principal | None, str, bool]:
    """Resolve (principal, owner_handle, is_admin) for a mutating preview call.

    - When ``DEVAI_REQUIRE_AUTH`` is on and no principal resolves → 401.
    - When auth is off (default) and no principal resolves, pin the caller to a
      per-request, non-reusable owner (``anon:<uuid>``) instead of collapsing
      to a shared ``"operator"`` — so anonymous previews can't cross-read or
      reuse one another. This preserves today's "works without auth" behavior
      while closing the IDOR-by-shared-owner gap (CODE-10).
    """
    principal = await _resolve_principal(request)
    owner = _owner_handle(principal)
    if owner is None:
        if _require_auth(request):
            raise HTTPException(status_code=401, detail="authentication required")
        owner = f"anon:{uuid.uuid4().hex[:12]}"
    is_admin = bool(principal and "admin" in (principal.roles or []))
    return principal, owner, is_admin


async def _read_scope(request: Request) -> tuple[str | None, bool]:
    """Resolve (owner_handle, is_admin) for a read/ownership-checked call.

    Returns ``(None, False)`` for an unresolved anonymous caller when auth is
    off; the service then treats every row as foreign (404) for that caller,
    which is correct — they own only the per-request previews they started.
    When auth is on and no principal resolves → 401.
    """
    principal = await _resolve_principal(request)
    owner = _owner_handle(principal)
    if owner is None and _require_auth(request):
        raise HTTPException(status_code=401, detail="authentication required")
    is_admin = bool(principal and "admin" in (principal.roles or []))
    return owner, is_admin


class StartPreviewBody(BaseModel):
    repo: str = Field(..., min_length=1, max_length=200)
    ref: str = Field("main", max_length=200)

    @field_validator("repo")
    @classmethod
    def _validate_repo(cls, v: str) -> str:
        v = (v or "").strip()
        if not _REPO_RE.match(v):
            raise ValueError("repo must be 'owner/name' using letters, digits, '.', '_', '-', '/'")
        return v

    @field_validator("ref")
    @classmethod
    def _validate_ref(cls, v: str) -> str:
        v = (v or "main").strip() or "main"
        if v.startswith("-") or not _REF_RE.match(v):
            raise ValueError("ref must be a git ref of letters, digits, '.', '_', '-', '/' (no leading '-')")
        return v


@router.post("/start", status_code=201)
async def start_preview(request: Request, body: StartPreviewBody) -> dict[str, Any]:
    _, owner, _ = await _principal_owner(request)
    try:
        return await _service(request).start(body.repo, body.ref, owner=owner)
    except PreviewError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.get("")
async def list_previews(request: Request, mine: bool = True) -> list[dict[str, Any]]:
    """List previews. Default is owner-scoped (CODE-10): a caller sees only
    their own previews unless they are an admin requesting the global view."""
    owner, is_admin = await _read_scope(request)
    if not mine and is_admin:
        return await _service(request).list(owner=None)
    return await _service(request).list(owner=owner)


@router.get("/{session_id}")
async def get_preview(request: Request, session_id: str) -> dict[str, Any]:
    owner, is_admin = await _read_scope(request)
    row = await _service(request).get(session_id, owner=owner, is_admin=is_admin)
    if row is None:
        raise HTTPException(status_code=404, detail=f"preview {session_id!r} not found")
    return row


@router.post("/{session_id}/verify")
async def verify_preview(request: Request, session_id: str, heal: bool = True) -> dict[str, Any]:
    """Diagnose (and by default self-heal) a preview's bring-up failures."""
    _, owner, is_admin = await _principal_owner(request)
    try:
        res = await _service(request).verify(session_id, heal=heal, owner=owner, is_admin=is_admin)
    except PreviewError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    if res is None:
        raise HTTPException(status_code=404, detail=f"preview {session_id!r} not found")
    return res


@router.post("/{session_id}/stop")
async def stop_preview(request: Request, session_id: str) -> dict[str, Any]:
    _, owner, is_admin = await _principal_owner(request)
    if not await _service(request).stop(session_id, owner=owner, is_admin=is_admin):
        raise HTTPException(status_code=404, detail=f"preview {session_id!r} not found")
    return {"stopped": session_id}


__all__ = ["router"]
