"""Opt-in authentication gate for mutating requests.

The DevAI backend normally trusts the auth-bff + Istio edge: traffic is
authenticated at the gateway, which stamps ``X-Forwarded-*`` identity
headers, and the API reads those (see :mod:`devai.identity`). That's fine
as long as the pod is only ever reachable through that edge.

When ``DEVAI_REQUIRE_AUTH`` is set, this module adds defense in depth: a
mutating request (POST/PUT/PATCH/DELETE) must resolve to a real principal
or it's rejected with 401 — so a pod accidentally reachable without the
edge can't be driven by an anonymous caller. Reads stay open, and webhook
deliveries are exempt because they authenticate with an HMAC signature,
not a principal.

The default is off, so wiring this in changes nothing until it's enabled.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from devai.identity import extract_principal

if TYPE_CHECKING:
    from fastapi import Request

    from devai.identity import Principal

_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Prefixes that must stay reachable without a principal:
#   /webhook/*  authenticate via HMAC signature (devai.webhook.signature)
#   health/ready probes are unauthenticated by design
_EXEMPT_PREFIXES = ("/webhook/", "/healthz", "/readyz")


def _is_exempt(path: str) -> bool:
    return any(path == p or path.startswith(p) for p in _EXEMPT_PREFIXES)


def needs_auth_check(method: str, path: str) -> bool:
    """True when this request should be gated (mutating + not exempt)."""
    return method.upper() in _MUTATING_METHODS and not _is_exempt(path)


async def enforce_auth(request: Request) -> JSONResponse | None:
    """Return a 401 response if the request must be authenticated and isn't.

    Returns ``None`` (let the request proceed) when the gate is disabled,
    the route is exempt/non-mutating, or a principal resolves.
    """
    config = getattr(request.app.state, "config", None)
    if config is None or not getattr(config, "require_auth", False):
        return None
    if not needs_auth_check(request.method, request.url.path):
        return None
    try:
        principal = await extract_principal(request)
    except Exception:  # noqa: BLE001 — identity lookup failure must not 500
        principal = None
    if principal is None:
        return JSONResponse({"detail": "authentication required"}, status_code=401)
    return None


async def require_principal(request: Request) -> Principal:
    """Resolve the caller's :class:`~devai.identity.Principal` or raise 401.

    This is the per-route counterpart to :func:`enforce_auth` (which is the
    blanket middleware gate). Mutating handlers that must know *who* the caller
    is — and must refuse anonymous access regardless of the global
    ``require_auth`` flag — depend on this directly::

        from devai.authz import require_principal

        @router.post("/api/pipeline/{run_id}/approve")
        async def approve(run_id: str, request: Request):
            principal = await require_principal(request)
            ...

    It reuses :func:`devai.identity.extract_principal`, so it honors the same
    trust rules (auth-bff shared-secret gate, forwarded-header stripping,
    session-cookie decryption). A failure inside identity resolution is
    treated as "no principal" — the request is rejected with 401, never
    leaked as a 500. Callers must NOT fall back to a literal ``operator``
    principal for mutating calls; this helper is the single rejection point.
    """
    try:
        principal = await extract_principal(request)
    except Exception:  # noqa: BLE001 — identity lookup failure must not 500
        principal = None
    if principal is None:
        raise HTTPException(status_code=401, detail="authentication required")
    return principal


__all__ = ["enforce_auth", "needs_auth_check", "require_principal"]

async def resolve_principal(request: Request) -> Principal | None:
    """Resolve the caller, gated by the ``require_auth`` flag.

    Behavior-neutral while ``DEVAI_REQUIRE_AUTH`` is False: returns the
    resolved principal or ``None`` for anonymous callers. With the flag on it
    delegates to :func:`require_principal`, which 401s anonymous callers.
    """
    config = getattr(request.app.state, "config", None)
    if config is not None and getattr(config, "require_auth", False):
        return await require_principal(request)
    try:
        return await extract_principal(request)
    except Exception:  # noqa: BLE001 — identity lookup failure must not 500
        return None


def may_see_run(request: Request, principal: Principal | None, run: dict | None) -> bool:
    """Whether `principal` may see `run`, per its owning team.

    Delegates to the same ``can_dispatch`` rule the write paths use, so an
    unscoped run (no team_id) and a principal with no teams both keep the
    global access they had before.
    """
    team_service = getattr(request.app.state, "team_service", None)
    if team_service is None:
        return True
    return bool(team_service.can_dispatch(principal, (run or {}).get("team_id") or ""))


async def authorize_run_read(request: Request, run: dict | None) -> Principal | None:
    """Enforce team membership on a run READ, answering 404 rather than 403.

    A 403 would confirm that a run id exists in another team, so reads report
    the run as simply absent.
    """
    principal = await resolve_principal(request)
    if not may_see_run(request, principal, run):
        raise HTTPException(status_code=404, detail="Run not found")
    return principal
