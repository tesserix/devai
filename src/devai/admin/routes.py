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

from fastapi import APIRouter, Depends, HTTPException, Query, Request

import devai.authz
from devai.admin.openpanel import fetch_overview
from devai.admin.service import active_user_totals, active_users_timeseries, signin_count
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


async def _db(request: Request):
    """The analytics Postgres handle, or None when unreachable."""
    from devai.analytics.routes import get_db as analytics_db

    return await analytics_db(request)


@router.get("/overview")
async def overview(request: Request, days: int = Query(30, ge=1, le=365)) -> dict[str, Any]:
    """Platform activity: active users, sign-ins, and per-user LLM usage.

    Two different sources, deliberately kept distinct in the payload:
    `active_users`/`user_activity` are exact (audit_log), while `by_user`
    carries real spend from the Redis usage ledger.
    """
    database = await _db(request)
    ledger = getattr(request.app.state, "usage_ledger", None)

    by_user: list[dict[str, Any]] = []
    if ledger is not None:
        try:
            by_user = await ledger.by_user("")
        except Exception:  # noqa: BLE001
            logger.debug("admin: ledger by_user failed", exc_info=True)

    return {
        "days": days,
        "active_users": await active_users_timeseries(database, days),
        "signins": await signin_count(database, days),
        "user_activity": await active_user_totals(database, days),
        "by_user": by_user,
        "enabled": True,
    }


@router.get("/openpanel")
async def openpanel(request: Request, days: int = Query(30, ge=1, le=365)) -> dict[str, Any]:
    """Page-level browsing stats. Reports disabled until configured."""
    return await fetch_overview(getattr(request.app.state, "config", None), days)
