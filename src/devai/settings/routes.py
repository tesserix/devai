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


async def _authorize(request: Request, principal: Principal, scope: Scope, scope_id: str) -> None:
    """Enforce who may WRITE a connector at a given scope.

    - user:   only your own scope.
    - team:   you must be a TEAM ADMIN of that team (membership alone is not
              enough — a shared team credential is admin-managed).
    - org:    you must be an ORG ADMIN (admin of a team in that org).
    - tenant/global: platform admin only.
    A global ``admin`` role overrides all of these.
    """
    is_admin = "admin" in (principal.roles or [])
    if scope == Scope.USER:
        if scope_id and scope_id not in (principal.uid, principal.email):
            raise HTTPException(status_code=403, detail="Cannot manage another user's settings")
        return
    if is_admin:
        return
    team_service = getattr(request.app.state, "team_service", None)
    user_key = principal.uid or principal.email
    if scope == Scope.TEAM:
        ok = team_service is not None and await team_service.is_team_admin(scope_id, user_key)
        if not ok:
            raise HTTPException(status_code=403, detail="Team admin role required to manage team settings")
        return
    if scope == Scope.ORG:
        ok = team_service is not None and await team_service.is_org_admin(scope_id, user_key)
        if not ok:
            raise HTTPException(status_code=403, detail="Org admin role required to manage org settings")
        return
    # tenant / global
    raise HTTPException(status_code=403, detail="Admin role required for tenant/global settings")


def _scope_default(principal: Principal) -> tuple[Scope, str]:
    return Scope.USER, (principal.uid or principal.email)


# ── catalog ───────────────────────────────────────────────────────────────


def _kagent_supported_catalog() -> list[dict[str, str]]:
    """The operator-curated menu of supported (provider, model) variants
    (config kagent_catalog). The set a user may enable from."""
    import json as _json

    from devai.config import settings as base

    out: list[dict[str, str]] = []
    try:
        for it in _json.loads(str(getattr(base, "kagent_catalog", "") or "[]")):
            if isinstance(it, dict) and it.get("provider") and it.get("model"):
                out.append(
                    {"suffix": str(it.get("suffix", "")), "provider": str(it["provider"]), "model": str(it["model"])}
                )
    except (ValueError, TypeError):
        pass
    return out


async def _kagent_user_enabled_models(principal: Principal, svc: Any) -> list[str]:
    """The model ids THIS user enabled for kagent (their kagent connector's
    prefs.enabled_models). Drives which variants get provisioned."""
    email = getattr(principal, "email", "") or ""
    lister = getattr(svc, "list_user_connectors_by_email", None)
    if lister is None or not email:
        return []
    try:
        for c in await lister(email):
            if c.connector_key == "kagent":
                em = (c.prefs or {}).get("enabled_models")
                if isinstance(em, list):
                    return [str(m) for m in em]
    except Exception:  # noqa: BLE001 — never break the catalog on a settings read
        return []
    return []


async def _kagent_catalog_public(principal: Principal, svc: Any) -> dict[str, Any]:
    """The kagent runtime model catalog for the Settings UI: the supported menu,
    plus THIS user's effective on/off (their connector overlay) and which models
    they've enabled — so the UI can render per-model toggles."""
    from devai.config import settings as base
    from devai.settings.overlay import build_overlay

    overlay = await build_overlay(base, principal, svc)
    return {
        "enabled": bool(getattr(overlay, "kagent_enabled", False)),
        "passthrough": bool(getattr(base, "kagent_passthrough", False)),
        "models": _kagent_supported_catalog(),
        "enabled_models": await _kagent_user_enabled_models(principal, svc),
    }


def _require_kagent_service(request: Request) -> None:
    """Gate the active-variants endpoint to a trusted internal service.

    The kagent-agent-sync reconciler presents the SAME shared bearer the MCP Hub
    uses (``DEVAI_MCP_HUB_SERVICE_TOKEN``); devai-api already accepts it via
    :func:`identity._principal_from_service_bearer`, yielding a ``service``-role
    principal. Reusing it means one key, one rotation — no new secret. The
    endpoint returns non-sensitive data (the union of which models users enabled,
    no creds), so any authenticated service identity is sufficient."""
    from devai.identity import _principal_from_service_bearer

    principal = _principal_from_service_bearer(request)
    if principal is None or "service" not in (principal.roles or []):
        raise HTTPException(status_code=401, detail="service token required")


def _kagent_default_model(catalog: list[dict[str, str]]) -> str:
    """The fallback model when a user turned kagent ON but hasn't picked any model
    yet — so "kagent on" alone provisions at least one usable variant instead of
    nothing. The first catalog entry for ``kagent_model_provider`` (else the first
    entry overall). Mirrors the dispatch fallback in job_runner."""
    from devai.config import settings as base

    provider = str(getattr(base, "kagent_model_provider", "anthropic") or "anthropic").lower()
    for e in catalog:
        if e["provider"].lower() == provider:
            return e["model"]
    return catalog[0]["model"] if catalog else ""


@router.get("/kagent/active-variants")
async def kagent_active_variants(request: Request) -> dict[str, Any]:
    """The variants kagent-agent-sync should provision RIGHT NOW: the union of
    every user's enabled models (∩ the supported catalog), for users whose kagent
    switch is on. Service-token gated. Returns a ready-to-use ``param`` string."""
    _require_kagent_service(request)
    svc = _svc(request)
    catalog = _kagent_supported_catalog()
    default_model = _kagent_default_model(catalog)
    wanted: set[str] = set()
    for c in await svc.list_all_by_key("kagent"):
        if str(c.provider).lower() != "on":  # only users who turned kagent on
            continue
        em = (c.prefs or {}).get("enabled_models")
        picked = [str(m) for m in em] if isinstance(em, list) else []
        # kagent on but no model chosen → provision the platform default so the
        # user isn't left with a live switch and zero working variants.
        wanted.update(picked or ([default_model] if default_model else []))
    variants = [e for e in catalog if e["model"] in wanted]
    return {
        "variants": variants,
        "param": ",".join(f"{e['suffix']}:kagent-mc-{e['suffix']}" for e in variants),
    }


async def _kagent_live_model_status() -> dict[str, str] | None:
    """Map model id → ``running`` | ``provisioning`` from the kagent controller's
    ``/api/agents`` (each item carries the model id + a Ready condition). Returns
    None when the controller is unreachable/unconfigured, so the caller degrades
    to enablement-only status. Plain HTTP over the path devai already uses for
    A2A dispatch — no extra RBAC."""
    from devai.config import settings as base

    url = str(getattr(base, "kagent_url", "") or "").rstrip("/")
    if not url:
        return None
    try:
        import httpx

        async with httpx.AsyncClient(timeout=6.0) as client:
            resp = await client.get(f"{url}/api/agents")
            resp.raise_for_status()
            data = resp.json().get("data") or []
    except Exception:  # noqa: BLE001 — status is best-effort; never break the page
        logger.debug("kagent live status: controller unreachable", exc_info=True)
        return None

    out: dict[str, str] = {}
    for it in data:
        model = it.get("model")
        if not model:
            continue
        conds = ((it.get("agent") or {}).get("status") or {}).get("conditions") or []
        ready = bool(it.get("deploymentReady")) or any(
            str(c.get("type")) == "Ready" and str(c.get("status")) == "True" for c in conds
        )
        # 'running' wins if any variant on this model is Ready.
        if out.get(model) != "running":
            out[model] = "running" if ready else "provisioning"
    return out


@router.get("/kagent/runtime-status")
async def kagent_runtime_status(request: Request) -> dict[str, Any]:
    """Per-model live status for the Settings UI: ``running`` (pod Ready),
    ``provisioning`` (enabled, pod not up yet), ``off`` (not enabled), or
    ``enabled`` (enabled but the controller is unreachable so readiness is
    unknown). Lets the panel show the pod actually coming up after a toggle."""
    principal = await _require_principal(request)
    svc = _svc(request)
    enabled = set(await _kagent_user_enabled_models(principal, svc))
    live = await _kagent_live_model_status()
    catalog = _kagent_supported_catalog()
    models: dict[str, str] = {}
    for e in catalog:
        m = e["model"]
        if m not in enabled:
            models[m] = "off"
        elif live is None:
            models[m] = "enabled"  # controller unknown → fall back to intent
        else:
            models[m] = live.get(m, "provisioning")  # enabled but no variant yet
    return {"available": live is not None, "models": models}


@router.get("/catalog")
async def get_catalog(request: Request) -> dict[str, Any]:
    """The connector catalog (field definitions) + capability flags + the kagent
    runtime model catalog (enabled = this user's effective setting)."""
    principal = await _require_principal(request)
    svc = _svc(request)
    return {
        "connectors": catalog_public(),
        "secrets_writable": await svc.secrets_writable(),
        "has_db": svc.has_db,
        "kagent": await _kagent_catalog_public(principal, svc),
    }


# ── list / read ─────────────────────────────────────────────────────────────


@router.get("")
async def list_my_settings(request: Request) -> dict[str, Any]:
    """List connectors visible to the caller: their user scope + their teams
    + (if admin) tenant/global. Secret values are never included."""
    principal = await _require_principal(request)
    svc = _svc(request)
    team_service = getattr(request.app.state, "team_service", None)
    user_key = principal.uid or principal.email
    is_admin = "admin" in (principal.roles or [])

    scopes: list[tuple[Scope, str]] = [(Scope.USER, user_key)]
    for team_id in principal.team_ids or []:
        scopes.append((Scope.TEAM, team_id))
    for org_id in getattr(principal, "org_ids", []) or []:
        scopes.append((Scope.ORG, org_id))
    if principal.tenant_id:
        scopes.append((Scope.TENANT, principal.tenant_id))
    scopes.append((Scope.GLOBAL, ""))

    # Which scopes can THIS caller write? (drives the UI's scope selector +
    # whether an inherited connector shows as editable). Team/org need admin.
    writable_scopes: list[dict[str, str]] = [{"scope": "user", "scope_id": user_key, "label": "Just me"}]
    for team_id in principal.team_ids or []:
        if is_admin or (team_service is not None and await team_service.is_team_admin(team_id, user_key)):
            writable_scopes.append({"scope": "team", "scope_id": team_id, "label": f"Team {team_id}"})
    for org_id in getattr(principal, "org_ids", []) or []:
        if is_admin or (team_service is not None and await team_service.is_org_admin(org_id, user_key)):
            writable_scopes.append({"scope": "org", "scope_id": org_id, "label": f"Org {org_id}"})

    out: list[dict[str, Any]] = []
    # shared[connector_key] = the broadest non-user scope that already provides
    # it, so the UI can warn before a user sets a personal override.
    shared: dict[str, dict[str, str]] = {}
    for scope, scope_id in scopes:
        for c in await svc.list_connectors(scope, scope_id):
            d = c.public_dict()
            out.append(d)
            if scope != Scope.USER and c.connector_key not in shared:
                shared[c.connector_key] = {
                    "scope": scope.value,
                    "scope_id": scope_id,
                    "provider": c.provider,
                    "updated_by": c.updated_by,
                }
    return {
        "connectors": out,
        "secrets_writable": await svc.secrets_writable(),
        "writable_scopes": writable_scopes,
        "shared": shared,
    }


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
    await _authorize(request, principal, scope, scope_id)

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


# ── LLM capabilities (what's connected + how each agent routes) ─────────────


@router.get("/llm/capabilities")
async def llm_capabilities(request: Request) -> dict[str, Any]:
    """The LLM providers CONNECTED for the caller (their own connectors +
    inherited platform config) and how each agent role resolves to a concrete
    ``(provider, model)`` — so the UI can show the system knows how it's
    configured. Read-only; never returns keys."""
    principal = await _require_principal(request)
    svc = _svc(request)

    from devai.config import settings as base_settings

    overlay = base_settings
    try:
        from devai.settings.llm_resolver import PrincipalLLMResolver

        overlay = await PrincipalLLMResolver(base_settings, svc).settings_for_email(
            principal.email or principal.uid
        )
    except Exception:  # noqa: BLE001 — degrade to platform view, never 500
        logger.warning("llm capabilities: overlay resolution failed — using platform config", exc_info=True)

    from devai.adapters.llm.capabilities import describe_capabilities

    return describe_capabilities(overlay)


# ── delete ──────────────────────────────────────────────────────────────────


# ── MCP marketplace ─────────────────────────────────────────────────────────


def _mcp_catalog_entry(rec: Any) -> dict[str, Any]:
    """One registry MCPServer → a marketplace card (no secrets, UI-safe)."""
    raw = getattr(rec, "raw", None) or {}
    meta = raw.get("metadata", {}) if isinstance(raw, dict) else {}
    labels = meta.get("labels", {}) if isinstance(meta, dict) else {}
    tools = raw.get("tools") if isinstance(raw, dict) else None
    endpoint = (raw.get("endpoint") if isinstance(raw, dict) else "") or getattr(rec, "url", "") or ""
    connect = raw.get("connect", {}) if isinstance(raw, dict) else {}
    is_catalog = labels.get("mcp.devai.io/catalog") == "true" or bool(raw.get("catalog"))
    # First-party DevAI servers are always-on platform infra (built-in). Catalog
    # templates are user-connectable. Everything else (a real shared external
    # leg) is connectable too.
    is_builtin = not is_catalog and (
        "devai.svc.cluster.local" in str(endpoint) or labels.get("devai.io/source") == "devai"
    )
    return {
        "name": getattr(rec, "name", ""),
        "display_name": (raw.get("displayName") if isinstance(raw, dict) else "") or getattr(rec, "name", ""),
        "description": getattr(rec, "description", "") or (raw.get("description", "") if isinstance(raw, dict) else ""),
        "endpoint": str(endpoint),
        "auth_mode": (raw.get("authMode") if isinstance(raw, dict) else "") or "none",
        "transport": getattr(rec, "type", "") or (raw.get("transport") if isinstance(raw, dict) else "") or "streamable-http",
        "category": labels.get("devai.io/category", ""),
        "tools": list(tools) if isinstance(tools, list) else [],
        "tool_count": len(tools) if isinstance(tools, list) else 0,
        "builtin": bool(is_builtin),
        "catalog": bool(is_catalog),
        # Connect hints for the UI: how to authenticate + native transport.
        "auth_kind": connect.get("authKind", "") or labels.get("mcp.devai.io/auth-kind", ""),
        "native": connect.get("native", "") or labels.get("mcp.devai.io/native", ""),
        "credential": connect.get("credential", ""),
        "docs": connect.get("docs", ""),
    }


@router.get("/mcp/marketplace")
async def mcp_marketplace(request: Request) -> dict[str, Any]:
    """Browse every MCP server in the registry — the marketplace a user picks
    from to connect their own servers (`builtin: false` entries are
    connectable; `builtin: true` are the platform's always-on servers)."""
    await _require_principal(request)
    client = getattr(request.app.state, "registry_client", None)
    if client is None:
        from devai.registry import create_registry_client

        client = create_registry_client(getattr(request.app.state, "config", None))
    try:
        servers = client.list_mcp_servers()
    except Exception:  # noqa: BLE001 — empty marketplace beats a 500
        logger.warning("settings: MCP marketplace registry read failed", exc_info=True)
        servers = []
    entries = [_mcp_catalog_entry(s) for s in servers]
    return {
        "servers": entries,
        "connectable": [e for e in entries if not e["builtin"]],
        "builtin": [e for e in entries if e["builtin"]],
    }


@router.delete("/connectors/{scope}/{scope_id}/{connector_key}/secrets/{field_key}")
async def clear_secret(
    scope: str, scope_id: str, connector_key: str, field_key: str, request: Request
) -> dict[str, str]:
    """Remove ONE secret field (e.g. just the Anthropic key) from a connector."""
    principal = await _require_principal(request)
    svc = _svc(request)
    try:
        scope_enum = Scope(scope)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid scope") from None
    sid = "" if scope_id == "-" else scope_id
    await _authorize(request, principal, scope_enum, sid)
    instance_id = request.query_params.get("instance_id", "default")
    ok = await svc.clear_secret_field(
        scope_enum, sid, connector_key, field_key, instance_id, actor=principal.email
    )
    return {"status": "cleared" if ok else "not_found"}


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
    await _authorize(request, principal, scope_enum, sid)
    instance_id = request.query_params.get("instance_id", "default")
    ok = await svc.delete_connector(scope_enum, sid, connector_key, instance_id, actor=principal.email)
    return {"status": "deleted" if ok else "not_found"}


__all__ = ["router"]
