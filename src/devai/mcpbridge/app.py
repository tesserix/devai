"""devai-mcp-bridge — serves stdio MCP servers over streamable-https.

Reads the registry's catalog templates (``mcp.devai.io/catalog: "true"`` with a
``spec.stdio`` launch recipe), and mounts each at ``/bridge/<name>`` as a
streamable-http MCP server. The hub federates those endpoints like any other
downstream, so a user who connects (say) draw.io or Postgres gets its tools
without anything ever speaking stdio outside this pod.

Per request, an ASGI middleware lifts the ``x-mcp-secret`` / ``x-mcp-prefs``
headers into a contextvar; the per-name server spawns the
stdio process with that secret substituted into its launch env. Only commands
on the allowlist may run.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any

from devai.mcp_stateless import stateless_asgi
from devai.mcpbridge.runner import LaunchSpec, command_allowed, stdio_session
from devai.registry import create_registry_client

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
    try:
        records = client.list_mcp_servers()
    except Exception:  # noqa: BLE001
        logger.warning("mcpbridge: registry read failed; no servers mounted", exc_info=True)
        return {}
    return _specs_from_records(records)


def _specs_from_records(records: Any) -> dict[str, LaunchSpec]:
    """Pure parse: registry records → {segment: LaunchSpec} (no I/O)."""
    out: dict[str, LaunchSpec] = {}
    for rec in records:
        raw = getattr(rec, "raw", None) or {}
        if not isinstance(raw, dict):
            continue
        # The registry flattens spec to the top of `raw` and drops
        # metadata.labels, so detect a catalog template via the spec `catalog`
        # flag (falling back to the label if a future client preserves it).
        meta = raw.get("metadata", {})
        labels = meta.get("labels", {}) if isinstance(meta, dict) else {}
        is_catalog = bool(raw.get("catalog")) or labels.get("mcp.devai.io/catalog") == "true"
        if not is_catalog:
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


async def load_catalog_specs_resilient(
    settings: Any, *, attempts: int = 12, delay: float = 6.0
) -> dict[str, LaunchSpec]:
    """Catalog specs with a bounded startup retry on registry unavailability.

    The bridge mounts its servers ONCE at boot. If the registry is briefly
    unreachable then (a cold start, an agentregistry roll), a single read would
    leave the bridge serving 0 servers permanently. Retry the read — distinct
    from an empty-but-healthy result — so a transient hiccup can't zero the
    bridge for the pod's whole life. Gives up after ``attempts`` (~72s) and
    serves empty rather than crashlooping.
    """
    last_err: Exception | None = None
    for i in range(1, attempts + 1):
        try:
            client = create_registry_client(settings)
            if client is None:
                raise RuntimeError("registry URL is not configured")
            records = client.list_mcp_servers()
            return _specs_from_records(records)
        except Exception as e:  # noqa: BLE001
            last_err = e
            logger.warning("mcpbridge: registry read failed (attempt %d/%d) — retrying in %.0fs", i, attempts, delay)
            await asyncio.sleep(delay)
    logger.error("mcpbridge: registry unreachable after %d attempts (%s) — serving 0 servers", attempts, last_err)
    return {}


def _env_for(spec: LaunchSpec) -> dict[str, str]:
    """Resolve the launch env from the current request's secret/prefs."""
    prefs = {str(k): str(v) for k, v in _current_prefs().items()}
    return spec.resolve_env(secret=_SECRET.get(), prefs=prefs)


def _build_bridge_server(name: str, spec: LaunchSpec) -> Any:
    """A low-level MCP Server proxying to the spawned stdio process for ``name``.

    A fresh stdio session is opened per call within the SAME task (anyio
    cancel scopes are task-bound — a cross-task cached session crashes the
    SDK). npx caches the package after the first spawn so re-spawns are quick.
    """
    import mcp.types as t
    from mcp.server.lowlevel import Server

    async def _list_tools(_ctx: Any, _params: Any) -> Any:
        async with stdio_session(spec, _env_for(spec)) as s:
            return t.ListToolsResult(tools=list((await s.list_tools()).tools))

    async def _call_tool(_ctx: Any, params: Any) -> Any:
        async with stdio_session(spec, _env_for(spec)) as s:
            result = await s.call_tool(params.name, params.arguments or {})
        content = getattr(result, "content", None)
        if content is not None:
            return t.CallToolResult(content=content, is_error=bool(getattr(result, "isError", False)))
        return t.CallToolResult(content=[t.TextContent(type="text", text=str(result))])

    return Server(f"devai-bridge-{name}", on_list_tools=_list_tools, on_call_tool=_call_tool)


def create_bridge_app(settings: Any) -> Any:
    """The bridge ASGI app: /healthz + /bridge/<name> per catalog stdio server."""
    from fastapi import FastAPI, Request
    from starlette.responses import JSONResponse

    allowed = [c.strip() for c in str(getattr(settings, "mcpbridge_allowed_commands", "npx")).split(",") if c.strip()]
    mounted: dict[str, LaunchSpec] = {}

    @asynccontextmanager
    async def lifespan(app: Any) -> AsyncIterator[None]:  # noqa: ANN001
        from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

        specs = await load_catalog_specs_resilient(settings)
        entered: list[Any] = []
        for name, spec in specs.items():
            if not command_allowed(spec.command, allowed):
                logger.warning("mcpbridge: %r command %r not allowed — skipped", name, spec.command)
                continue
            try:
                server = _build_bridge_server(name, spec)
                manager = StreamableHTTPSessionManager(app=server, stateless=True)
                cm = manager.run()
                await cm.__aenter__()
                entered.append(cm)

                app.mount(f"/bridge/{name}", stateless_asgi(manager.handle_request, normalize_mount=True))
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
