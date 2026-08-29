"""User-activity recording for the admin overview.

DevAI never observes a login in production: auth-bff terminates OAuth
outside the pod and forwards `X-Forwarded-*` identity headers, so the
backend sees authenticated *requests*, not sign-in *moments*. What it can
count exactly is therefore DAILY ACTIVE USERS — distinct principals that
made at least one request on a given day. That's what the admin page
labels it, rather than presenting it as a login count.

Rows land in the existing append-only `audit_log` table (no new schema —
repo policy keeps SQL out of this repo). A Redis `SET NX EX` guard collapses a user's
whole day to a single row, so this costs one write per user per day, not
one per request, and holds across pods.

Recording is best-effort in the strictest sense: every failure path
returns False and writes nothing. A telemetry miss must never fail a
user's request.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from starlette.middleware.base import BaseHTTPMiddleware

if TYPE_CHECKING:
    from devai.identity import Principal

logger = logging.getLogger(__name__)

ACTION_ACTIVE = "user_active"
ACTION_LOGIN = "login"

_DEDUP_KEY = "devai:activity:{day}:{actor}"
_DEDUP_TTL_SECONDS = 48 * 60 * 60  # outlives the day it guards, then reaps itself


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


async def record_active(app_state: Any, principal: Principal | None) -> bool:
    """Record one `user_active` row for this principal, once per day."""
    actor = getattr(principal, "email", "") or getattr(principal, "uid", "")
    if not actor or not isinstance(actor, str):
        return False
    # Normalized so the stored value matches the dedup key below — an IdP
    # that varies casing must not split one user into two rollup rows.
    actor = actor.lower()
    # Synthetic principals are machines, not users on a dashboard.
    auth_provider = getattr(principal, "auth_provider", "") or ""
    if (
        auth_provider == "system"
        or auth_provider.startswith("webhook:")
        or auth_provider == "service-token"
    ):
        return False

    database = getattr(app_state, "analytics_db", None)
    redis = getattr(app_state, "activity_redis", None)
    if database is None or redis is None:
        return False

    day = _today()
    try:
        claimed = await redis.set(
            _DEDUP_KEY.format(day=day, actor=actor.lower()),
            "1",
            nx=True,
            ex=_DEDUP_TTL_SECONDS,
        )
    except Exception:  # noqa: BLE001
        logger.debug("activity: dedup guard unavailable — skipping row", exc_info=True)
        return False
    if not claimed:
        return False

    try:
        await database.audit(
            action=ACTION_ACTIVE,
            actor=actor,
            actor_type="user",
            details={
                "day": day,
                "uid": getattr(principal, "uid", "") or "",
                "tenant_id": getattr(principal, "tenant_id", "") or "",
                "auth_provider": getattr(principal, "auth_provider", "") or "",
            },
        )
    except Exception:  # noqa: BLE001
        logger.debug("activity: audit write failed", exc_info=True)
        return False
    return True


async def record_login(app_state: Any, actor: str) -> bool:
    """Record an explicit sign-in. Local-dev only — in production auth-bff
    terminates OAuth outside this pod, so no login reaches us and the admin
    page sources sign-ins from OpenPanel instead."""
    if not actor:
        return False
    database = getattr(app_state, "analytics_db", None)
    if database is None:
        return False
    try:
        await database.audit(
            action=ACTION_LOGIN,
            actor=actor,
            actor_type="user",
            details={"day": _today(), "source": "local"},
        )
    except Exception:  # noqa: BLE001
        logger.debug("activity: login audit failed", exc_info=True)
        return False
    return True


class ActivityMiddleware(BaseHTTPMiddleware):
    """Record the caller as active for today, then get out of the way."""

    # Probes and static assets say nothing about a human being present.
    _SKIP_PREFIXES = ("/healthz", "/readyz", "/webhook/", "/metrics")

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        path = request.url.path
        if any(path.startswith(p) for p in self._SKIP_PREFIXES):
            return response
        try:
            from devai.identity import extract_principal

            principal = await extract_principal(request)
            await record_active(request.app.state, principal)
        except Exception:  # noqa: BLE001
            logger.debug("activity middleware: recording skipped", exc_info=True)
        return response
