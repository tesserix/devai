"""Identity propagation for DevAI.

The auth-bff (services/auth-bff) terminates Google/Keycloak OAuth and
forwards every downstream request with these stamped headers:

    X-Forwarded-User    user's email (canonical)
    X-Forwarded-Email   same as User (for symmetry with the proxy)
    X-Forwarded-Uid     stable subject ID from GIP / Keycloak
    X-Forwarded-Tenant  GIP tenant id (alm vs sre pool)

This module turns those headers (or the dashboard's Redis session) into a
``Principal`` and threads it the rest of the way: into the pipeline
state, into every A2A message, into the persisted task. Without it the
backend knows *which agents talked* but not *which human asked* — which
is fine for development and impossible for audit.

The principal is intentionally a plain dataclass with a ``to_dict`` /
``from_dict`` pair so it crosses the LangGraph ``ALMState`` (TypedDict
of JSON-compatible values) and ``DevAITask.agent_context`` cleanly.

Use::

    principal = await extract_principal(request)
    # request handlers pass principal to the pipeline boundary;
    # everything downstream reads it from state.
"""

from __future__ import annotations

import hmac
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import Request

logger = logging.getLogger(__name__)

SYSTEM_PRINCIPAL_EMAIL = "system@devai"
WEBHOOK_PRINCIPAL_EMAIL = "webhook@devai"


@dataclass(slots=True)
class Principal:
    """Who initiated this work.

    ``email`` is the canonical handle the rest of the system uses — it's
    what appears in A2A ``triggered_by`` fields, in audit logs, and on
    the dashboard timeline. ``uid`` is the immutable subject ID; keep it
    around so audit records survive an email change.

    A Principal can also represent non-human triggers — a webhook
    arriving from GitHub, a cron firing the SRE scanner — by setting
    ``auth_provider`` to ``"webhook"`` / ``"system"`` and using a
    synthetic email like ``webhook:tesserix/devai#42``. That keeps the
    field non-null at every hop, which is what downstream code wants.
    """

    email: str
    uid: str = ""
    tenant_id: str = ""
    pool: str = ""
    auth_provider: str = "unknown"  # google | keycloak | github | webhook | system
    roles: list[str] = field(default_factory=list)
    display_name: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "email": self.email,
            "uid": self.uid,
            "tenant_id": self.tenant_id,
            "pool": self.pool,
            "auth_provider": self.auth_provider,
            "roles": list(self.roles),
            "display_name": self.display_name,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> Principal | None:
        if not data:
            return None
        return cls(
            email=data.get("email", ""),
            uid=data.get("uid", ""),
            tenant_id=data.get("tenant_id", ""),
            pool=data.get("pool", ""),
            auth_provider=data.get("auth_provider", "unknown"),
            roles=list(data.get("roles") or []),
            display_name=data.get("display_name", ""),
        )

    @classmethod
    def system(cls) -> Principal:
        """Fallback principal for code paths with no real caller (cron, internal jobs)."""
        return cls(email=SYSTEM_PRINCIPAL_EMAIL, auth_provider="system")

    @classmethod
    def webhook(cls, provider: str, sender_login: str, sender_email: str = "") -> Principal:
        """Build a webhook-trigger principal from SCM payload sender data.

        GitHub/GitLab/ADO webhooks all carry a sender block. We use
        the login as the display handle and synthesize an email if the
        provider didn't surface one (most don't on private accounts).
        """
        return cls(
            email=sender_email or f"{sender_login}@{provider}",
            uid=sender_login,
            auth_provider=f"webhook:{provider}",
            display_name=sender_login,
        )


def new_trace_id() -> str:
    """Mint a fresh trace ID.

    We use a hex-encoded UUID4 (32 chars) rather than ULIDs here because
    LangSmith, OpenTelemetry, and most log aggregators happily accept it.
    """
    return uuid.uuid4().hex


def _forward_trusted(request: Request) -> bool:
    """Whether ``X-Forwarded-*`` identity headers should be trusted.

    When ``DEVAI_AUTH_BFF_SHARED_SECRET`` is configured, the auth-bff must
    echo it in the ``X-Auth-Bff-Secret`` header; a request without a
    matching secret is treated as un-forwarded (its X-Forwarded-* headers
    are ignored). When no secret is configured, forwarded headers are
    trusted unconditionally — the original behavior.
    """
    config = getattr(getattr(request, "app", None), "state", None)
    secret = getattr(getattr(config, "config", None), "auth_bff_shared_secret", "") if config else ""
    if not secret:
        return True
    provided = request.headers.get("x-auth-bff-secret", "")
    return bool(provided) and hmac.compare_digest(provided, secret)


async def extract_principal(request: Request) -> Principal | None:
    """Resolve the current Principal from the request, or None.

    Order of preference:

    1. ``X-Forwarded-User`` / ``X-Forwarded-Email`` headers stamped by the
       auth-bff proxy (services/auth-bff/internal/proxy/proxy.go). When
       traffic comes through the bff this is the authoritative path.
    2. The ``devai_session`` cookie — the dashboard FastAPI app stores
       session metadata in Redis at ``devai:session:{session_id}`` after
       OAuth (Keycloak or GitHub). Used when the dashboard talks to the
       backend directly (same-origin) without the bff.
    3. None — caller decides whether to fall back to ``Principal.system()``
       or to refuse the request.
    """
    # 1. auth-bff stamped headers — trusted only when they actually came
    #    from the bff (see _forward_trusted). Otherwise ignore them and fall
    #    through, so a direct caller can't spoof identity by setting headers.
    fwd_email = request.headers.get("x-forwarded-user") or request.headers.get("x-forwarded-email")
    if fwd_email and _forward_trusted(request):
        return Principal(
            email=fwd_email,
            uid=request.headers.get("x-forwarded-uid", ""),
            tenant_id=request.headers.get("x-forwarded-tenant", ""),
            pool=request.headers.get("x-forwarded-pool", ""),
            auth_provider="auth-bff",
            display_name=request.headers.get("x-forwarded-name", "") or fwd_email,
        )

    # 2. dashboard session cookie → Redis lookup
    session_id = request.cookies.get("devai_session")
    if session_id:
        try:
            state_manager = getattr(request.app.state, "state_manager", None)
            if state_manager is not None:
                raw = await state_manager.redis.get(f"devai:session:{session_id}")
                if raw:
                    data = json.loads(raw)
                    return Principal(
                        email=data.get("user_email", "") or data.get("user_login", ""),
                        uid=data.get("user_login", ""),
                        auth_provider=data.get("auth_provider", "unknown"),
                        roles=list(data.get("roles") or []),
                        display_name=data.get("user_name", "") or data.get("user_login", ""),
                    )
        except Exception:
            # Session lookup must never block a request — degrade to None.
            logger.debug("session lookup failed for cookie", exc_info=True)

    return None


def trace_id_from_request(request: Request) -> str:
    """Honor an upstream trace id if one was stamped, else mint a new one.

    Order of preference:
      1. ``traceparent`` (W3C Trace Context) — first hex group after the
         version byte is the trace id. The auth-bff proxy can be wired up
         to forward this when it lands.
      2. ``X-Request-Id`` — common nginx/istio convention.
      3. fresh hex UUID4 as a last resort.
    """
    traceparent = request.headers.get("traceparent", "")
    if traceparent:
        # 00-<32-hex trace_id>-<16-hex span_id>-<2-hex flags>
        parts = traceparent.split("-")
        if len(parts) >= 2 and len(parts[1]) == 32:
            return parts[1]

    req_id = request.headers.get("x-request-id", "")
    if req_id:
        return req_id

    return new_trace_id()


__all__ = [
    "Principal",
    "SYSTEM_PRINCIPAL_EMAIL",
    "WEBHOOK_PRINCIPAL_EMAIL",
    "extract_principal",
    "new_trace_id",
    "trace_id_from_request",
]
