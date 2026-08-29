"""Admin-only analytics endpoints.

Mounted at `/api/admin/*` by `devai.webhook.app.create_app`. Read-only.

This file gates all routes at the router level with a dependency that FastAPI
resolves before any handler in this router runs. Authorization is stated once
instead of repeated (and eventually forgotten) per endpoint. The dashboard's
admin tab renders only when an answer is 200 — the API is the authority,
never client-side email check.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

import devai.authz
from devai.identity import Principal

logger = logging.getLogger(__name__)

_ADMIN_ROLES = frozenset({"admin", "platform-admin"})


async def require_admin(request: Request) -> Principal:
    """Gate admin routes: non-anonymous users must have admin role.

    Raises HTTPException(401) if anonymous or identity resolution fails,
    or HTTPException(403) if authenticated but without admin role.
    """
    principal = await devai.authz.require_principal(request)

    if not (_ADMIN_ROLES & set(principal.roles)):
        raise HTTPException(status_code=403, detail="admin role required")
    return principal


router = APIRouter(
    prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)]
)


@router.get("/overview")
async def overview(request: Request) -> dict[str, Any]:
    """Platform activity: active users, sign-ins, per-user LLM usage."""
    return {"active_users": [], "signins": 0, "by_user": [], "enabled": False}
