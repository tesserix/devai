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


# ── model discovery ──────────────────────────────────────────────────────────

# (provider, used_user_key) → (expires_monotonic, models). Discovery hits
# external APIs; a short TTL keeps the Settings UI snappy without hammering.
_MODELS_CACHE: dict[tuple[str, bool], tuple[float, list[dict[str, str]]]] = {}
_MODELS_TTL_S = 60.0


@router.get("/models/{provider}")
async def list_provider_models(provider: str, request: Request) -> dict[str, Any]:
    """Models the caller can use on ``provider``, evaluated against THEIR
    keys (Settings overlay; platform credentials only as fallback). Secret
    values never appear in the response.
    """
    import time as _time

    principal = await _require_principal(request)
    svc = _svc(request)

    from devai.adapters.llm.factory import KNOWN_PROVIDERS, create_llm_adapter

    provider = provider.lower()
    if provider not in KNOWN_PROVIDERS:
        raise HTTPException(status_code=400, detail=f"unknown provider: {provider} (known: {', '.join(KNOWN_PROVIDERS)})")

    from devai.config import settings as base_settings
    from devai.settings.overlay import PrincipalSettingsOverlay, build_overlay

    overlay = await build_overlay(base_settings, principal, svc)
    has_user_key = isinstance(overlay, PrincipalSettingsOverlay)

    cache_key = (provider, has_user_key)
    cached = _MODELS_CACHE.get(cache_key)
    now = _time.monotonic()
    if cached and cached[0] > now and not has_user_key:
        # Only platform-credential results are shared across callers.
        return {"provider": provider, "configured": True, "models": cached[1], "cached": True}

    adapter = create_llm_adapter(overlay, provider=provider)
    try:
        configured = adapter.provider_name != "noop" or provider == "noop"
        models = await adapter.list_models() if configured else []
    finally:
        try:
            await adapter.close()
        except Exception:  # noqa: BLE001
            pass

    if not has_user_key and configured:
        if len(_MODELS_CACHE) > 32:
            _MODELS_CACHE.clear()
        _MODELS_CACHE[cache_key] = (now + _MODELS_TTL_S, models)

    # Per-model enable/disable state from the caller's LLM connector
    # (prefs.enabled_models). No list stored → everything enabled.
    raw_enabled = getattr(overlay, "llm_enabled_models", None) or []
    enabled_list = [str(m).strip() for m in raw_enabled if str(m).strip()]
    models_out = [
        {**m, "enabled": (not enabled_list) or m.get("id") in enabled_list}
        for m in models
    ]
    return {
        "provider": provider,
        "configured": configured,
        "models": models_out,
        "enabled_models": enabled_list,
    }


# ── trial status ─────────────────────────────────────────────────────────────


@router.get("/trial")
async def trial_status(request: Request) -> dict[str, Any]:
    """The caller's trial-allowance state — drives the depletion banner.

    ``applicable`` is false when the user already has their own LLM
    connector (trial irrelevant) or strict mode is off.
    """
    principal = await _require_principal(request)
    svc = _svc(request)

    from devai.config import settings as base_settings
    from devai.settings.trial import get_trial_meter

    email = principal.email or principal.uid
    meter = get_trial_meter(base_settings)
    status = await meter.status(email)

    has_own = False
    try:
        from devai.settings.llm_resolver import PrincipalLLMResolver

        resolver = PrincipalLLMResolver(base_settings, svc)
        has_own = await resolver.resolve(principal) is not None
    except Exception:  # noqa: BLE001
        logger.warning("trial status: connector check failed", exc_info=True)

    strict = bool(getattr(base_settings, "llm_require_user_connector", False))
    return {
        **status,
        "has_own_connector": has_own,
        "applicable": strict and not has_own,
    }


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
    ok = await svc.delete_connector(scope_enum, sid, connector_key, instance_id, actor=principal.email)
    return {"status": "deleted" if ok else "not_found"}


__all__ = ["router"]
