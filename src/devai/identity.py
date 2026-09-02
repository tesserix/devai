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

import base64
import hmac
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC
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
    # Human teams this principal belongs to (resolved from the team_members
    # table at request time). Empty = no team scoping; the user sees the
    # global/unscoped view (back-compat with every pre-teams trigger path).
    team_ids: list[str] = field(default_factory=list)
    # Orgs this principal belongs to — the distinct teams.org_id of their
    # teams (resolved with team_ids). This is the VERIFIABLE org boundary used
    # for org-scoped connectors: membership can't be spoofed (it's derived from
    # team_members), so an org connector never resolves cross-org.
    org_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "email": self.email,
            "uid": self.uid,
            "tenant_id": self.tenant_id,
            "pool": self.pool,
            "auth_provider": self.auth_provider,
            "roles": list(self.roles),
            "display_name": self.display_name,
            "team_ids": list(self.team_ids),
            "org_ids": list(self.org_ids),
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
            team_ids=list(data.get("team_ids") or []),
            org_ids=list(data.get("org_ids") or []),
        )

    @property
    def primary_team_id(self) -> str:
        """The team a new task is attributed to when the caller didn't pick one."""
        return self.team_ids[0] if self.team_ids else ""

    @property
    def user_scope_id(self) -> str:
        """Stable settings/usage owner key, qualified by the auth tenant.

        Identity-provider subjects are only guaranteed unique inside their
        issuer/tenant. Keeping the historical unqualified shape when no tenant
        exists preserves local and service identities, while authenticated
        tenant principals cannot collide with the same subject in another
        tenant.
        """
        subject = (self.uid or self.email).strip()
        tenant = self.tenant_id.strip()
        return f"{tenant}:{subject}" if tenant and subject else subject

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
    are ignored). The match is a constant-time compare of two *non-empty*
    values — an empty provided secret can never equal an empty configured
    secret (we never reach the compare in that case), so a header-less
    request is never silently trusted under a configured secret.

    When no secret is configured, behavior is controlled by
    ``DEVAI_TRUST_FORWARDED_WITHOUT_SECRET`` (default True): True keeps the
    original behavior (trust the edge unconditionally); False FAILS CLOSED so
    forwarded identity is never trusted without a matched non-empty secret.
    """
    cfg = getattr(getattr(request, "app", None), "state", None)
    cfg = getattr(cfg, "config", None) if cfg else None
    secret = getattr(cfg, "auth_bff_shared_secret", "").strip() if cfg else ""
    if not secret:
        # No shared secret. Trust the edge only if the deployment opts in
        # (default) — otherwise fail closed (never empty==empty match).
        return bool(getattr(cfg, "trust_forwarded_without_secret", True))
    provided = request.headers.get("x-auth-bff-secret", "")
    return bool(provided) and hmac.compare_digest(provided, secret)


# Identity headers the auth-bff stamps. When a request's forwarded identity
# can't be trusted, we strip ALL of these from the ASGI scope so no later
# code path (or accidental re-read) can derive identity or an admin role from
# them. ``X-Auth-Bff-Secret`` is stripped too so it never leaks downstream.
_FORWARDED_IDENTITY_HEADERS = frozenset(
    {
        b"x-forwarded-user",
        b"x-forwarded-email",
        b"x-forwarded-uid",
        b"x-forwarded-tenant",
        b"x-forwarded-pool",
        b"x-forwarded-name",
        b"x-forwarded-roles",
        b"x-auth-bff-secret",
    }
)


def _strip_forwarded_identity(request: Request) -> None:
    """Remove untrusted ``X-Forwarded-*`` identity headers from the ASGI scope.

    Starlette's ``request.headers`` is immutable, but it's a view over
    ``request.scope["headers"]`` (a list of ``(name, value)`` byte tuples).
    Rewriting that list in place ensures any downstream handler that reads
    forwarded identity sees nothing — defense in depth for the case where the
    secret check failed but a caller still set spoofed headers. Never raises.
    """
    try:
        scope = getattr(request, "scope", None)
        if not isinstance(scope, dict):
            return
        headers = scope.get("headers")
        if not headers:
            return
        scope["headers"] = [
            (name, value) for (name, value) in headers if name.lower() not in _FORWARDED_IDENTITY_HEADERS
        ]
    except Exception:  # noqa: BLE001 — header scrubbing must never break a request
        logger.debug("failed to strip untrusted forwarded headers", exc_info=True)


def _principal_from_service_bearer(request: Request) -> Principal | None:
    """The MCP Hub's service identity from its shared bearer token, or None.

    Compares ``Authorization: Bearer <x>`` against the configured
    ``mcp_hub_service_token`` with :func:`hmac.compare_digest` (no timing
    leak). Both sides read the SAME key from the devai-api-secrets
    ExternalSecret, so rotation is one GCP SM update. Never raises.
    """
    try:
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            return None
        presented = auth[7:].strip()
        cfg = getattr(getattr(request, "app", None), "state", None)
        expected = str(getattr(getattr(cfg, "config", None), "mcp_hub_service_token", "") or "") if cfg else ""
        if len(expected) < 16 or not presented:
            return None
        if not hmac.compare_digest(presented.encode(), expected.encode()):
            return None
        return Principal(
            email="mcp-hub@service.devai.internal",
            uid="svc-mcp-hub",
            auth_provider="service-token",
            roles=["service"],
            display_name="DevAI MCP Hub",
        )
    except Exception:  # noqa: BLE001 — identity resolution must never 500
        logger.debug("service-bearer check failed", exc_info=True)
        return None


def extract_adk_runtime_principal(request: Request) -> Principal | None:
    """Resolve an AgentGateway-admitted A2A workload, or return ``None``."""
    try:
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            return None
        presented = auth[7:].strip()
        state = getattr(getattr(request, "app", None), "state", None)
        expected = str(getattr(getattr(state, "config", None), "adk_runtime_service_token", "") or "")
        if len(expected) < 16 or not presented:
            return None
        if not hmac.compare_digest(presented.encode(), expected.encode()):
            return None

        subject = request.headers.get("x-adk-workload-subject", "").strip()
        client_id = request.headers.get("x-adk-workload-client-id", "").strip()
        if not subject or not client_id or len(subject) > 256 or len(client_id) > 256:
            return None
        return Principal(
            email=f"agentgateway:{client_id}",
            uid=subject,
            auth_provider="agentgateway-runtime",
            roles=["service", "agentgateway.runtime"],
            display_name=client_id,
        )
    except Exception:  # noqa: BLE001 -- identity resolution must never 500
        logger.debug("ADK runtime service-bearer check failed", exc_info=True)
        return None


async def extract_principal(request: Request) -> Principal | None:
    """Resolve the current Principal from the request, or None.

    Order of preference:

    1. The MCP Hub's service bearer (``Authorization: Bearer`` matching the
       shared ``DEVAI_MCP_HUB_SERVICE_TOKEN``) — how the Hub authenticates
       toward the per-domain MCP servers it federates (/mcp/scm, /mcp/gitops).
       Identity is terminated at the Hub, so the Hub presents its OWN service
       identity here, never the original caller's.
    2. ``X-Forwarded-User`` / ``X-Forwarded-Email`` headers stamped by the
       auth-bff proxy (services/auth-bff/internal/proxy/proxy.go). When
       traffic comes through the bff this is the authoritative path.
    3. The ``devai_session`` cookie — the dashboard FastAPI app stores
       session metadata in Redis at ``devai:session:{session_id}`` after
       OAuth (Keycloak or GitHub). Used when the dashboard talks to the
       backend directly (same-origin) without the bff.
    4. None — caller decides whether to fall back to ``Principal.system()``
       or to refuse the request.
    """
    # 0. MCP Hub service bearer — constant-time match against the shared
    #    token. Only honored when the operator configured a non-trivial token
    #    (≥16 chars), so an empty/placeholder setting can never be "matched"
    #    by an empty Authorization header.
    principal = _principal_from_service_bearer(request)
    if principal is not None:
        return principal

    # 1. auth-bff stamped headers — trusted only when they actually came
    #    from the bff (see _forward_trusted). Otherwise STRIP them and fall
    #    through, so a direct caller can't spoof identity (or an admin role)
    #    by setting headers, and no later handler can read the spoofed values.
    fwd_email = request.headers.get("x-forwarded-user") or request.headers.get("x-forwarded-email")
    if fwd_email:
        if _forward_trusted(request):
            principal = Principal(
                email=fwd_email,
                uid=request.headers.get("x-forwarded-uid", ""),
                tenant_id=request.headers.get("x-forwarded-tenant", ""),
                pool=request.headers.get("x-forwarded-pool", ""),
                auth_provider="auth-bff",
                roles=[
                    role.strip() for role in request.headers.get("x-forwarded-roles", "").split(",") if role.strip()
                ],
                display_name=request.headers.get("x-forwarded-name", "") or fwd_email,
            )
            await _enrich_teams(principal, request)
            return principal
        # Forwarded identity present but the secret check failed — never trust
        # it; scrub the headers so nothing downstream picks them up.
        _strip_forwarded_identity(request)

    # 2. dashboard session cookie → Redis lookup
    session_id = request.cookies.get("devai_session")
    if session_id:
        try:
            state_manager = getattr(request.app.state, "state_manager", None)
            if state_manager is not None:
                raw = await state_manager.redis.get(f"devai:session:{session_id}")
                if raw:
                    data = json.loads(raw)
                    principal = Principal(
                        email=data.get("user_email", "") or data.get("user_login", ""),
                        uid=data.get("user_login", ""),
                        auth_provider=data.get("auth_provider", "unknown"),
                        roles=list(data.get("roles") or []),
                        display_name=data.get("user_name", "") or data.get("user_login", ""),
                    )
                    await _enrich_teams(principal, request)
                    return principal
        except Exception:
            # Session lookup must never block a request — degrade to None.
            logger.debug("session lookup failed for cookie", exc_info=True)

    # 3. auth-bff encrypted session cookie. The bff stores the whole session in
    #    an AES-GCM cookie (services/auth-bff/internal/session/cookie.go) — NOT
    #    a Redis id — so the Redis lookup above misses it. In prod the dashboard
    #    proxies /api straight to devai-api (bypassing the bff), so this is the
    #    only identity present. Decrypt it with the shared key.
    if session_id:  # same devai_session cookie value, re-used as the bff blob
        principal = _principal_from_bff_cookie(session_id, request)
        if principal is not None:
            await _enrich_teams(principal, request)
            return principal

    return None


def _principal_from_bff_cookie(cookie_value: str, request: Request) -> Principal | None:
    """Decrypt an auth-bff AES-GCM session cookie into a Principal, or None.

    Mirrors the bff's encode (base64url(nonce || ciphertext), AES-GCM, JSON
    payload {uid, email, tenant_id, pool, iat, exp}). Requires the same
    DEVAI_BFF_SESSION_ENCRYPT_KEY the bff uses. Never raises."""
    cfg = getattr(getattr(request, "app", None), "state", None)
    key = getattr(getattr(cfg, "config", None), "bff_session_encrypt_key", "") if cfg else ""
    if not key:
        return None
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        key_bytes = key.encode("utf-8")
        if len(key_bytes) not in (16, 24, 32):
            return None
        raw = base64.urlsafe_b64decode(cookie_value + "=" * (-len(cookie_value) % 4))
        if len(raw) < 12:
            return None
        nonce, ciphertext = raw[:12], raw[12:]
        plaintext = AESGCM(key_bytes).decrypt(nonce, ciphertext, None)
        data = json.loads(plaintext)
    except Exception:
        # Wrong key, tampered cookie, or a real Redis session id (not a bff
        # blob) — either way, not a valid bff session. Degrade to None.
        return None

    exp = data.get("exp", "")
    if exp:
        try:
            from datetime import datetime

            when = datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
            if when.tzinfo is None:
                when = when.replace(tzinfo=UTC)
            if datetime.now(UTC) > when:
                return None  # expired
        except Exception:
            pass
    email = data.get("email", "")
    uid = data.get("uid", "")
    if not (email or uid):
        return None
    return Principal(
        email=email or uid,
        uid=uid,
        tenant_id=data.get("tenant_id", ""),
        pool=data.get("pool", ""),
        auth_provider="auth-bff",
        display_name=email or uid,
    )


async def _enrich_teams(principal: Principal, request: Request) -> None:
    """Best-effort: attach the principal's team_ids from a TeamService.

    The service is optional (`request.app.state.team_service`). If it's
    absent or errors, the principal simply has no teams — the unscoped
    view — so teams are purely additive and never break auth.

    Also grants the "admin" role to configured platform owners
    (DEVAI_ADMIN_EMAILS): every user-principal path runs through here, and
    bff-stamped identities otherwise carry NO roles, which would leave the
    deployment without a single admin (analytics' global views, the by-user
    usage table, …).
    """
    _apply_admin_role(principal)
    team_service = getattr(request.app.state, "team_service", None)
    if team_service is None:
        return
    try:
        user_key = principal.uid or principal.email
        principal.team_ids = await team_service.teams_for(user_key)
        if hasattr(team_service, "orgs_for"):
            principal.org_ids = await team_service.orgs_for(user_key)
    except Exception:  # noqa: BLE001
        logger.debug("team enrichment failed for %s", principal.email, exc_info=True)


def _apply_admin_role(principal: Principal) -> None:
    """Add "admin" to the principal's roles when its email is allowlisted."""
    try:
        from devai.config import settings

        admins = {e.strip().lower() for e in (settings.admin_emails or "").split(",") if e.strip()}
        email = (principal.email or "").lower()
        if email and email in admins and "admin" not in principal.roles:
            principal.roles.append("admin")
    except Exception:  # noqa: BLE001 — role enrichment must never break auth
        logger.debug("admin role enrichment failed", exc_info=True)


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
    "extract_adk_runtime_principal",
    "extract_principal",
    "new_trace_id",
    "trace_id_from_request",
]
