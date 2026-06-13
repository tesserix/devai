"""MCP OAuth routes — the Connect-with-OAuth flow for hosted SaaS MCP servers.

  POST /api/settings/mcp/oauth/start  {server}  → {authorize_url}
  GET  /api/settings/mcp/oauth/callback?code&state → provisions the connector,
                                                      redirects back to Settings

The flow state (PKCE verifier, endpoints, client) is held in Redis keyed by an
unguessable ``state`` for 10 minutes, scoped to the starting principal. On
callback the code is exchanged and the REFRESH token is stored in the user's
GCP SM scope as a normal MCP connector (provider=oauth); per-leg federation
mints short-lived access tokens from it. Nothing here ever returns a token to
the browser.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from devai.identity import extract_principal
from devai.settings import mcp_oauth
from devai.settings.models import Scope

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings/mcp/oauth", tags=["settings", "oauth"])

_FLOW_PREFIX = "devai:mcp:oauth:flow:"
_FLOW_TTL = 600  # 10 minutes


def _redirect_uri(request: Request) -> str:
    base = str(getattr(getattr(request.app.state, "config", None), "public_base_url", "") or "").rstrip("/")
    return f"{base}/api/settings/mcp/oauth/callback"


def _redis(request: Request) -> Any:
    sm = getattr(request.app.state, "state_manager", None)
    return getattr(sm, "redis", None) if sm else None


def _catalog_server(request: Request, name: str) -> dict[str, Any] | None:
    """Resolve a catalog MCPServer (its mcp_url + optional oauth override)."""
    client = getattr(request.app.state, "registry_client", None)
    if client is None:
        from devai.registry import create_registry_client

        client = create_registry_client(getattr(request.app.state, "config", None))
    try:
        for rec in client.list_mcp_servers():
            if getattr(rec, "name", "") in (name, f"catalog-{name}-mcp"):
                raw = getattr(rec, "raw", None) or {}
                return {
                    "name": (raw.get("displayName") if isinstance(raw, dict) else "") or name,
                    "instance": name.replace("catalog-", "").replace("-mcp", ""),
                    "url": str(raw.get("endpoint", "")) if isinstance(raw, dict) else "",
                    "connect": raw.get("connect", {}) if isinstance(raw, dict) else {},
                }
    except Exception:  # noqa: BLE001
        logger.warning("oauth: catalog lookup failed for %r", name, exc_info=True)
    return None


@router.post("/start")
async def oauth_start(request: Request) -> dict[str, str]:
    """Begin OAuth for a catalog server; returns the consent URL to open."""
    principal = await extract_principal(request)
    if principal is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    redis = _redis(request)
    if redis is None:
        raise HTTPException(status_code=503, detail="OAuth flow store unavailable")

    body = await request.json()
    server_name = str(body.get("server") or "").strip()
    server = _catalog_server(request, server_name)
    if server is None or not server["url"]:
        raise HTTPException(status_code=404, detail=f"Unknown MCP server {server_name!r}")

    redirect_uri = _redirect_uri(request)
    oauth_cfg = (server.get("connect") or {}).get("oauth") or {}

    import httpx

    async with httpx.AsyncClient(timeout=30.0) as client:
        # Endpoints: catalog override wins; else discover from the server.
        if oauth_cfg.get("token_endpoint") and oauth_cfg.get("authorization_endpoint"):
            ep = mcp_oauth.OAuthEndpoints(
                authorization_endpoint=str(oauth_cfg["authorization_endpoint"]),
                token_endpoint=str(oauth_cfg["token_endpoint"]),
                registration_endpoint=str(oauth_cfg.get("registration_endpoint", "")),
                scopes=list(oauth_cfg.get("scopes") or []),
            )
        else:
            ep = await mcp_oauth.discover_endpoints(client, server["url"])
        if ep is None:
            raise HTTPException(status_code=400, detail="Could not resolve the server's OAuth endpoints")

        # Client id: catalog-provided, else dynamic registration.
        client_id = str(oauth_cfg.get("client_id") or "")
        client_secret = str(oauth_cfg.get("client_secret") or "")
        if not client_id and ep.registration_endpoint:
            try:
                reg = await mcp_oauth.register_client(client, ep.registration_endpoint, redirect_uri)
                client_id, client_secret = reg["client_id"], reg.get("client_secret", "")
            except Exception as e:  # noqa: BLE001
                raise HTTPException(status_code=400, detail=f"Dynamic client registration failed: {e}") from e
        if not client_id:
            raise HTTPException(status_code=400, detail="No client_id (no DCR support and none preconfigured)")

    verifier, challenge = mcp_oauth.pkce_pair()
    state = mcp_oauth.random_state()
    scopes = list(oauth_cfg.get("scopes") or ep.scopes or [])
    url = mcp_oauth.authorize_url(
        ep,
        client_id=client_id,
        redirect_uri=redirect_uri,
        state=state,
        code_challenge=challenge,
        scopes=scopes,
        resource=server["url"],
    )
    flow = {
        "uid": principal.uid or principal.email,
        "email": principal.email,
        "verifier": verifier,
        "token_endpoint": ep.token_endpoint,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "server_name": server["name"],
        "instance": server["instance"],
        "mcp_url": server["url"],
    }
    try:
        await redis.set(f"{_FLOW_PREFIX}{state}", json.dumps(flow), ex=_FLOW_TTL)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=503, detail="Could not persist OAuth flow") from e
    return {"authorize_url": url}


@router.get("/callback")
async def oauth_callback(request: Request) -> Any:
    """OAuth redirect target: exchange the code, provision the connector."""
    params = request.query_params
    state, code = params.get("state", ""), params.get("code", "")
    dash = str(getattr(getattr(request.app.state, "config", None), "public_base_url", "") or "").rstrip("/")
    err = params.get("error", "")
    if err:
        return RedirectResponse(f"{dash}/settings?mcp_oauth=error&detail={err}")
    redis = _redis(request)
    if not (state and code and redis is not None):
        return RedirectResponse(f"{dash}/settings?mcp_oauth=error&detail=missing_params")

    raw = await redis.get(f"{_FLOW_PREFIX}{state}")
    if not raw:
        return RedirectResponse(f"{dash}/settings?mcp_oauth=error&detail=expired")
    flow = json.loads(raw)
    await redis.delete(f"{_FLOW_PREFIX}{state}")

    svc = getattr(request.app.state, "settings_service", None)
    if svc is None:
        return RedirectResponse(f"{dash}/settings?mcp_oauth=error&detail=settings_unavailable")

    import httpx

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            tokens = await mcp_oauth.exchange_code(
                client,
                flow["token_endpoint"],
                code=code,
                redirect_uri=flow["redirect_uri"],
                client_id=flow["client_id"],
                code_verifier=flow["verifier"],
                client_secret=flow.get("client_secret", ""),
                resource=flow["mcp_url"],
                now=time.time(),
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("oauth: code exchange failed: %s", e)
        return RedirectResponse(f"{dash}/settings?mcp_oauth=error&detail=exchange_failed")

    if not tokens.refresh_token:
        # Without a refresh token the connection can't outlive the access token;
        # store the access token as the bearer and warn (still usable short-term).
        logger.warning("oauth: %s returned no refresh token", flow["server_name"])

    # Provision an MCP connector under the user's scope (provider=oauth).
    try:
        await svc.upsert_connector(
            scope=Scope.USER,
            scope_id=flow["uid"],
            connector_key="mcp",
            provider="oauth",
            instance_id=flow["instance"],
            prefs={
                "mcp_name": flow["server_name"],
                "mcp_url": flow["mcp_url"],
                "oauth_token_endpoint": flow["token_endpoint"],
                "oauth_client_id": flow["client_id"],
            },
            secret_values={"mcp_token": tokens.refresh_token or tokens.access_token},
            updated_by=flow["email"],
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("oauth: connector provisioning failed: %s", e)
        return RedirectResponse(f"{dash}/settings?mcp_oauth=error&detail=save_failed")

    return RedirectResponse(f"{dash}/settings?mcp_oauth=connected&server={flow['instance']}")


__all__ = ["router"]
