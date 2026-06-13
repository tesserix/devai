"""devai-mcp-bridge — serves stdio MCP servers over streamable-https.

Reads the registry's catalog templates (``mcp.devai.io/catalog: "true"`` with a
``spec.stdio`` launch recipe), and mounts each at ``/bridge/<name>`` as a
streamable-http MCP server. The hub federates those endpoints like any other
downstream, so a user who connects (say) draw.io or Postgres gets its tools
without anything ever speaking stdio outside this pod.

Per request, an ASGI middleware lifts the ``x-mcp-secret`` / ``x-mcp-prefs``
headers into a contextvar; the per-name server spawns (and briefly caches) the
stdio process with that secret substituted into its launch env. Only commands
on the allowlist may run.
"""

from __future__ import annotations

import hashlib
import json
import logging
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any

from devai.mcpbridge.runner import LaunchSpec, command_allowed, open_stdio_session

logger = logging.getLogger(__name__)

# Per-request credential material, set by the middleware before the MCP handler.
_SECRET: ContextVar[str] = ContextVar("mcpbridge_secret", default="")
_PREFS: ContextVar[dict[str, Any] | None] = ContextVar("mcpbridge_prefs", default=None)


def _current_prefs() -> dict[str, Any]:
    return _PREFS.get() or {}


def load_catalog_specs(client: Any) -> dict[str, LaunchSpec]:
    """name → LaunchSpec for every catalog server with a stdio launch recipe.

    ``name`` is the catalog short name (the path segment): ``catalog-drawio-mcp``
    served at ``/bridge/drawio`` → key ``drawio``. Best-effort: registry errors
    yield an empty map (the bridge serves nothing rather than crashing).
    """
    out: dict[str, LaunchSpec] = {}
    try:
        records = client.list_mcp_servers()
    except Exception:  # noqa: BLE001
        logger.warning("mcpbridge: registry read failed; no servers mounted", exc_info=True)
        return out
    for rec in records:
        raw = getattr(rec, "raw", None) or {}
        if not isinstance(raw, dict):
            continue
        meta = raw.get("metadata", {})
        labels = meta.get("labels", {}) if isinstance(meta, dict) else {}
        if labels.get("mcp.devai.io/catalog") != "true":
            continue
        stdio = raw.get("stdio")
        if not isinstance(stdio, dict) or not stdio.get("command"):
            continue
        endpoint = str(raw.get("endpoint", ""))
        seg = endpoint.rstrip("/").rsplit("/", 1)[-1] if endpoint else ""
        if not seg:
            continue
        out[seg] = LaunchSpec.from_spec(stdio)
    return out


class _SessionCache:
    """Reuse a spawned stdio session per (server, secret) for a short window."""

    def __init__(self) -> None:
        self._sessions: dict[str, tuple[Any, Any]] = {}  # key -> (session, aclose)

    @staticmethod
    def _key(name: str, secret: str) -> str:
        return f"{name}:{hashlib.sha256(secret.encode()).hexdigest()[:12]}"

    async def get(self, name: str, spec: LaunchSpec, secret: str, prefs: dict[str, Any]) -> Any:
        key = self._key(name, secret)
        cached = self._sessions.get(key)
        if cached is not None:
            return cached[0]
        env = spec.resolve_env(secret=secret, prefs={str(k): str(v) for k, v in prefs.items()})
        session, aclose = await open_stdio_session(spec, env)
        self._sessions[key] = (session, aclose)
        return session

    async def close(self) -> None:
        for _, aclose in self._sessions.values():
            try:
                await aclose()
            except Exception:  # noqa: BLE001
                pass
        self._sessions.clear()


def _build_bridge_server(name: str, spec: LaunchSpec, cache: _SessionCache) -> Any:
    """A low-level MCP Server proxying to the spawned stdio process for ``name``."""
    import mcp.types as t
    from mcp.server.lowlevel import Server

    server: Any = Server(f"devai-bridge-{name}")

    async def _session() -> Any:
        return await cache.get(name, spec, _SECRET.get(), _current_prefs())

    @server.list_tools()
    async def _list_tools() -> list[Any]:
        s = await _session()
        return list((await s.list_tools()).tools)

    @server.call_tool()
    async def _call_tool(tool_name: str, arguments: dict[str, Any]) -> Any:
        s = await _session()
        result = await s.call_tool(tool_name, arguments or {})
        content = getattr(result, "content", None)
        if content is not None:
            return content
        return [t.TextContent(type="text", text=str(result))]

    return server


def create_bridge_app(settings: Any) -> Any:
    """The bridge ASGI app: /healthz + /bridge/<name> per catalog stdio server."""
    from fastapi import FastAPI, Request
    from starlette.responses import JSONResponse

    allowed = [c.strip() for c in str(getattr(settings, "mcpbridge_allowed_commands", "npx")).split(",") if c.strip()]
    cache = _SessionCache()
    mounted: dict[str, LaunchSpec] = {}

    @asynccontextmanager
    async def lifespan(app: Any):  # noqa: ANN001
        from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

        from devai.registry import create_registry_client

        specs = load_catalog_specs(create_registry_client(settings))
        entered: list[Any] = []
        for name, spec in specs.items():
            if not command_allowed(spec.command, allowed):
                logger.warning("mcpbridge: %r command %r not allowed — skipped", name, spec.command)
                continue
            try:
                server = _build_bridge_server(name, spec, cache)
                manager = StreamableHTTPSessionManager(app=server, stateless=True)
                cm = manager.run()
                await cm.__aenter__()
                entered.append(cm)

                def _asgi(scope, receive, send, _m=manager):  # noqa: ANN001
                    if scope.get("type") == "http" and scope.get("path", "") in ("", scope.get("root_path", "")):
                        scope = dict(scope)
                        scope["path"] = "/"
                    return _m.handle_request(scope, receive, send)

                app.mount(f"/bridge/{name}", _asgi)
                mounted[name] = spec
                logger.info("mcpbridge: mounted /bridge/%s (%s)", name, spec.command)
            except Exception:  # noqa: BLE001
                logger.exception("mcpbridge: failed to mount /bridge/%s", name)
        logger.info("mcpbridge: %d stdio server(s) mounted", len(mounted))
        try:
            yield
        finally:
            for cm in entered:
                try:
                    await cm.__aexit__(None, None, None)
                except Exception:  # noqa: BLE001
                    pass
            await cache.close()

    app = FastAPI(title="devai-mcp-bridge", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> Any:  # noqa: ANN202
        return JSONResponse({"status": "ok", "service": "devai-mcp-bridge", "servers": sorted(mounted)})

    @app.middleware("http")
    async def _credentials(request: Request, call_next: Any) -> Any:
        # Lift per-request credential material into the contextvar the handlers
        # read. x-mcp-prefs is a JSON object of non-secret connector prefs.
        _SECRET.set(request.headers.get("x-mcp-secret", "") or "")
        raw_prefs = request.headers.get("x-mcp-prefs", "") or ""
        try:
            _PREFS.set(json.loads(raw_prefs) if raw_prefs else {})
        except Exception:  # noqa: BLE001
            _PREFS.set({})
        return await call_next(request)

    return app


__all__ = ["create_bridge_app", "load_catalog_specs"]
