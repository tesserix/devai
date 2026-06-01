"""Settings API — per-user/per-tenant connectors + secret provisioning.

All endpoints require an authenticated Principal (``extract_principal`` → 401).
A caller may only manage:
  - their own ``user`` scope (scope_id = their uid),
  - ``team`` scopes for teams they belong to,
  - ``tenant``/``global`` scopes only if they hold an ``admin`` role.

Secret values are accepted on write, pushed to the secrets backend (GCP SM),
and never returned or persisted in the app DB — reads only ever report which
fields *have* a secret set.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from devai.identity import Principal, extract_principal
from devai.settings.models import CONNECTOR_BY_KEY, Scope, catalog_public

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings", tags=["settings"])


def _svc(request: Request):
    svc = getattr(request.app.state, "settings_service", None)
    if svc is None:
        raise HTTPException(status_code=503, detail="Settings capability is not enabled")
    return svc


async def _require_principal(request: Request) -> Principal:
    principal = await extract_principal(request)
    if principal is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return principal


def _authorize(principal: Principal, scope: Scope, scope_id: str) -> None:
    """Enforce who may manage which scope."""
    is_admin = "admin" in (principal.roles or [])
    if scope == Scope.USER:
        if scope_id and scope_id not in (principal.uid, principal.email):
            raise HTTPException(status_code=403, detail="Cannot manage another user's settings")
        return
    if scope == Scope.TEAM:
        if not is_admin and scope_id not in (principal.team_ids or []):
            raise HTTPException(status_code=403, detail="Not a member of that team")
        return
    # tenant / global
    if not is_admin:
        raise HTTPException(status_code=403, detail="Admin role required for tenant/global settings")


def _scope_default(principal: Principal) -> tuple[Scope, str]:
    return Scope.USER, (principal.uid or principal.email)


# ── catalog ───────────────────────────────────────────────────────────────


@router.get("/catalog")
async def get_catalog(request: Request) -> dict[str, Any]:
    """The connector catalog (field definitions) + backend capability flags."""
    await _require_principal(request)
    svc = _svc(request)
    return {
        "connectors": catalog_public(),
        "secrets_writable": await svc.secrets_writable(),
        "has_db": svc.has_db,
    }


# ── list / read ─────────────────────────────────────────────────────────────


@router.get("")
async def list_my_settings(request: Request) -> dict[str, Any]:
    """List connectors visible to the caller: their user scope + their teams
    + (if admin) tenant/global. Secret values are never included."""
    principal = await _require_principal(request)
    svc = _svc(request)

    scopes: list[tuple[Scope, str]] = [(Scope.USER, principal.uid or principal.email)]
    for team_id in principal.team_ids or []:
        scopes.append((Scope.TEAM, team_id))
    if principal.tenant_id:
        scopes.append((Scope.TENANT, principal.tenant_id))
    scopes.append((Scope.GLOBAL, ""))

    out: list[dict[str, Any]] = []
    for scope, scope_id in scopes:
        for c in await svc.list_connectors(scope, scope_id):
            out.append(c.public_dict())
    return {"connectors": out, "secrets_writable": await svc.secrets_writable()}


# ── upsert ──────────────────────────────────────────────────────────────────


@router.post("/connectors")
async def upsert_connector(request: Request) -> dict[str, Any]:
    """Create or update a connector. Body:
    {scope, scope_id?, connector_key, provider, instance_id?, prefs{}, secrets{}}.
    """
    principal = await _require_principal(request)
    svc = _svc(request)
    body = await request.json()

    connector_key = body.get("connector_key", "")
    if connector_key not in CONNECTOR_BY_KEY:
        raise HTTPException(status_code=400, detail=f"unknown connector: {connector_key}")

    scope_raw = body.get("scope") or "user"
    try:
        scope = Scope(scope_raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"invalid scope: {scope_raw}") from None

    if scope == Scope.USER and not body.get("scope_id"):
        scope_id = principal.uid or principal.email
    else:
        scope_id = body.get("scope_id", "")
    _authorize(principal, scope, scope_id)

    secret_values = body.get("secrets") or {}
    if secret_values and not await svc.secrets_writable():
        raise HTTPException(
            status_code=409,
            detail="Secrets backend is read-only — set DEVAI_SECRETS_PROVIDER=gcp_sm and grant write IAM",
        )

    try:
        connector = await svc.upsert_connector(
            scope=scope,
            scope_id=scope_id,
            connector_key=connector_key,
            provider=body.get("provider", ""),
            instance_id=body.get("instance_id", "default"),
            prefs=body.get("prefs") or {},
            secret_values=secret_values,
            updated_by=principal.email,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001
        logger.exception("settings: upsert failed")
        raise HTTPException(status_code=500, detail=f"save failed: {e}") from e

    return {"status": "saved", "connector": connector.public_dict()}


# ── delete ──────────────────────────────────────────────────────────────────


@router.delete("/connectors/{scope}/{scope_id}/{connector_key}")
async def delete_connector(scope: str, scope_id: str, connector_key: str, request: Request) -> dict[str, str]:
    principal = await _require_principal(request)
    svc = _svc(request)
    try:
        scope_enum = Scope(scope)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid scope") from None
    # "-" means the global empty scope_id (path can't be empty).
    sid = "" if scope_id == "-" else scope_id
    _authorize(principal, scope_enum, sid)
    instance_id = request.query_params.get("instance_id", "default")
    ok = await svc.delete_connector(scope_enum, sid, connector_key, instance_id)
    return {"status": "deleted" if ok else "not_found"}


__all__ = ["router"]
