"""Per-user MCP federation — a caller's OWN connected MCP servers.

The registry-driven aggregate (``MCPHub``) is shared by everyone. This module
adds the missing half: the MCP servers a user connected in Settings → MCP
Server (stored per-user, token in their GCP SM scope). When a request carries a
principal, their personal servers are federated IN ADDITION to the shared
aggregate, namespaced ``usr-<instance>__<tool>`` and visible ONLY to that user.

Isolation: every lookup is keyed by the caller's email; one user's legs are
never listed or called for another. Endpoints are user-supplied, so each is
SSRF-screened (block loopback/link-local/private/metadata; allow public hosts —
unlike registry legs, external MCP servers are the whole point).

Lifecycle: legs are connected on demand and cached per email for a short TTL,
then closed. Best-effort throughout: a user's broken server drops from THEIR
surface with a log, never an exception into the hub.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import time
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from devai.mcphub.discovery import _is_blocked_ip
from devai.mcphub.downstream import DownstreamConnection, DownstreamError
from devai.mcphub.model import SEP, DownstreamSpec, namespaced

logger = logging.getLogger(__name__)

# Per-user leg cache TTL (seconds). list/call within the window reuse the
# live sessions; after it the legs are closed and re-resolved.
_TTL_SECONDS = 120.0


def check_external_endpoint(url: str) -> None:
    """SSRF guard for a USER-supplied MCP endpoint.

    Unlike registry legs (in-cluster suffix allowlist), users legitimately
    connect public servers — so the host allowlist is dropped, but the
    loopback/link-local/private/metadata IP blocks (incl. DNS-resolution
    rebinding) stay. Raises ValueError on rejection so callers fail closed.
    """
    if not url:
        raise ValueError("empty endpoint")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"unsupported scheme {parsed.scheme!r} (need http/https)")
    host = parsed.hostname
    if not host:
        raise ValueError("endpoint has no host")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if _is_blocked_ip(literal):
            raise ValueError(f"refusing non-routable address {host}")
        return
    try:
        infos = socket.getaddrinfo(host, parsed.port or None, proto=socket.IPPROTO_TCP)
    except OSError as e:
        raise ValueError(f"cannot resolve host {host!r}: {e}") from e
    for info in infos:
        try:
            resolved = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if _is_blocked_ip(resolved):
            raise ValueError(f"host {host!r} resolves to non-routable {info[4][0]}")


def _seg(instance_id: str) -> str:
    """A namespacing segment safe for the hub's ``server__tool`` scheme."""
    safe = "".join(c if (c.isalnum() or c == "-") else "-" for c in (instance_id or "default"))
    return f"usr-{safe.strip('-') or 'default'}"


def _spec_for(conn: dict[str, Any]) -> DownstreamSpec | None:
    """Build a DownstreamSpec from one resolved MCP connector dict.

    ``conn`` carries ``{instance_id, provider, mcp_url, mcp_token, mcp_auth_header}``
    (provider is the transport: streamable_http | sse). Returns None when the
    endpoint fails the SSRF guard.
    """
    url = str(conn.get("mcp_url") or "").strip()
    if not url:
        return None
    token = str(conn.get("mcp_token") or "").strip()
    headers: dict[str, str] = {}

    if _is_bridge_endpoint(url):
        # The endpoint is OUR in-cluster stdio bridge (a catalog stdio server).
        # It's a trusted internal service, so it bypasses the external SSRF
        # guard; the user's secret rides as x-mcp-secret for the bridge to
        # inject into the spawned process's env, plus any non-secret prefs.
        if token:
            headers["x-mcp-secret"] = token
        import json as _json

        prefs = {
            k: v
            for k, v in conn.items()
            if k not in ("mcp_url", "mcp_token", "mcp_auth_header", "instance_id", "provider")
        }
        if prefs:
            headers["x-mcp-prefs"] = _json.dumps(prefs)
    else:
        try:
            check_external_endpoint(url)
        except ValueError as e:
            logger.warning("mcphub: dropping personal MCP %r — %s", conn.get("instance_id"), e)
            return None
        is_oauth = str(conn.get("provider") or "").lower() == "oauth"
        header = str(conn.get("mcp_auth_header") or "").strip() or "Authorization"
        if token and not is_oauth:
            # A bare header name defaults to a Bearer scheme on Authorization;
            # a custom header (x-api-key, …) gets the raw token. OAuth connectors
            # store a REFRESH token, not a Bearer — the caller mints the access
            # token and sets Authorization itself (see _Manager._ensure).
            headers[header] = f"Bearer {token}" if header.lower() == "authorization" else token

    transport = "sse" if str(conn.get("provider") or "").lower() in ("sse",) else "streamable-http"
    return DownstreamSpec(
        name=_seg(str(conn.get("instance_id") or "default")),
        endpoint=url,
        transport=transport,
        auth_mode="header",
        headers=headers,
    )


def _is_bridge_endpoint(url: str) -> bool:
    """True for DevAI's own in-cluster stdio bridge (trusted internal service)."""
    host = urlparse(url).hostname or ""
    return host.startswith("devai-mcp-bridge") or "/bridge/" in url


async def _oauth_bearer(conn: dict[str, Any]) -> str:
    """Mint a fresh access token for an oauth MCP connector from its stored
    refresh token + token endpoint (provisioned by the OAuth callback)."""
    from devai.settings.mcp_oauth import access_token

    return await access_token(
        refresh_token=str(conn.get("mcp_token") or ""),
        token_endpoint=str(conn.get("oauth_token_endpoint") or ""),
        client_id=str(conn.get("oauth_client_id") or ""),
        client_secret=str(conn.get("oauth_client_secret") or ""),
        now=time.time(),
    )


@dataclass(slots=True)
class _Entry:
    expires: float
    conns: dict[str, DownstreamConnection]  # segment -> connection
    tools: dict[str, str] = field(default_factory=dict)  # namespaced name -> wire name
    descriptors: list[dict[str, Any]] = field(default_factory=list)  # {name, description, input_schema}


class PersonalLegs:
    """Per-user MCP leg manager: resolve → connect → list/call → expire."""

    def __init__(self, *, connect_timeout: float = 15.0, ttl: float = _TTL_SECONDS) -> None:
        self._timeout = connect_timeout
        self._ttl = ttl
        self._cache: dict[str, _Entry] = {}
        self._lock = asyncio.Lock()

    async def _resolve_connectors(self, email: str) -> list[dict[str, Any]]:
        from devai.settings.connections import user_connections

        return await user_connections(email, "mcp")

    async def _ensure(self, email: str) -> _Entry:
        now = time.monotonic()
        async with self._lock:
            cached = self._cache.get(email)
            if cached and cached.expires > now:
                return cached
            if cached:
                await self._close_entry(cached)
            connectors = await self._resolve_connectors(email)
            conns: dict[str, DownstreamConnection] = {}
            tools: dict[str, str] = {}
            descriptors: list[dict[str, Any]] = []
            for c in connectors:
                spec = _spec_for(c)
                if spec is None or spec.name in conns:
                    continue
                # OAuth connectors store a refresh token (mcp_token); mint a
                # short-lived Bearer access token for this leg.
                if str(c.get("provider") or "").lower() == "oauth":
                    bearer = await _oauth_bearer(c)
                    if not bearer:
                        logger.warning("mcphub: oauth MCP %r for %s — no access token", spec.name, email)
                        continue
                    spec.headers["Authorization"] = f"Bearer {bearer}"
                conn = DownstreamConnection(spec, headers=spec.headers, timeout=self._timeout)
                try:
                    await asyncio.wait_for(conn.connect(), timeout=self._timeout)
                    wire_tools = await conn.list_tools()
                except Exception as e:  # noqa: BLE001 — one bad personal leg drops, others serve
                    logger.warning("mcphub: personal MCP %r for %s unavailable: %s", spec.name, email, e)
                    continue
                conns[spec.name] = conn
                for wt in wire_tools:
                    wire = wt.get("name", "")
                    if not wire or SEP in spec.name:
                        continue
                    nsname = namespaced(spec.name, wire)
                    tools[nsname] = wire
                    descriptors.append(
                        {
                            "name": nsname,
                            "description": wt.get("description", "") or f"{wire} (your MCP: {spec.name})",
                            "input_schema": wt.get("inputSchema") or {"type": "object"},
                        }
                    )
            entry = _Entry(expires=now + self._ttl, conns=conns, tools=tools, descriptors=descriptors)
            self._cache[email] = entry
            logger.info("mcphub: %d personal MCP leg(s), %d tool(s) for %s", len(conns), len(tools), email)
            return entry

    async def tool_descriptors(self, email: str) -> list[dict[str, Any]]:
        """{name, description, input_schema} for every tool of the user's servers."""
        if not email or "@" not in email:
            return []
        try:
            entry = await self._ensure(email)
        except Exception:  # noqa: BLE001
            logger.warning("mcphub: personal leg resolve failed for %s", email, exc_info=True)
            return []
        return list(entry.descriptors)

    def owns(self, name: str) -> bool:
        """True if a namespaced name belongs to a personal leg (``usr-…__…``)."""
        seg, sep, _ = name.partition(SEP)
        return bool(sep) and seg.startswith("usr-")

    async def call(self, email: str, name: str, arguments: dict[str, Any]) -> Any:
        """Route a personal-leg ``tools/call`` to the right user connection."""
        entry = await self._ensure(email)
        seg, _, wire = name.partition(SEP)
        conn = entry.conns.get(seg)
        if conn is None or entry.tools.get(name) is None:
            raise DownstreamError(f"mcphub: personal MCP tool {name!r} is not available for {email}")
        return await conn.call_tool(wire, arguments or {})

    async def _close_entry(self, entry: _Entry) -> None:
        for conn in entry.conns.values():
            with suppress(Exception):
                await conn.close()

    async def close(self) -> None:
        async with self._lock:
            for entry in self._cache.values():
                await self._close_entry(entry)
            self._cache.clear()


__all__ = ["PersonalLegs", "check_external_endpoint"]
