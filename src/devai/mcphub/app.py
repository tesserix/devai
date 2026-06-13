"""ASGI entrypoint for ``devai-mcp-hub`` — the Agentic MCP surface.

A dedicated Deployment (docs/agentic/MCP-HUB.md decision §9.2): it shares the
``devai`` image (reusing auth/identity/registry-client) but is isolated so a
downstream MCP outage or a chatty client can't take down ``devai-api``.

Lifecycle:
  1. Build the registry-driven :class:`MCPHub`, do a first ``refresh()``.
  2. Mount the low-level MCP server over Streamable HTTP at ``/mcp``.
  3. Run a periodic refresh so newly-published MCPServers/Tools appear with zero
     redeploy (and a ``tools/list_changed`` fires on change — §6.4).
An HTTP middleware terminates the caller's identity and resolves their
:class:`ToolProfile` before each MCP request (per-caller budgeting, §6.5).

The ``mcp`` SDK is imported lazily in the lifespan, so a deployment without the
SDK starts cleanly and simply serves ``/healthz`` with the Hub unmounted.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

from devai.config import settings
from devai.mcphub.hub import MCPHub
from devai.mcphub.server import (
    build_hub_server,
    profile_for_principal,
    set_current_email,
    set_current_profile,
)

logger = logging.getLogger(__name__)


async def _periodic_refresh(hub: MCPHub, interval: float) -> None:
    """Re-discover + reconnect on a timer until cancelled."""
    while True:
        try:
            await asyncio.sleep(interval)
            await hub.refresh()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a refresh blip must not kill the loop
            logger.warning("mcphub: periodic refresh failed; will retry", exc_info=True)


def create_hub_app():  # noqa: ANN201 — FastAPI imported lazily
    """Build the FastAPI app hosting the DevAI MCP Hub."""
    from fastapi import FastAPI, Request
    from starlette.responses import JSONResponse

    from devai.registry.client import create_registry_client

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        registry = create_registry_client(settings)
        if registry is None:
            logger.warning("mcphub: DEVAI_REGISTRY_URL unset — Hub has no downstreams to federate")
        app.state.hub = None
        app.state._refresh_task = None
        app.state._mcp_cm = None

        if not settings.mcp_hub_enabled or registry is None:
            yield
            return

        hub = MCPHub(
            registry,
            service_token=settings.mcp_hub_service_token,
            connect_timeout=settings.mcp_hub_connect_timeout,
        )
        app.state.hub = hub
        await hub.refresh()

        # Mount the MCP server over Streamable HTTP, mirroring the proven mount
        # in webhook/app.py (session_manager.run() lifecycle).
        try:
            from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

            server = build_hub_server(hub)
            session_manager = StreamableHTTPSessionManager(app=server)
            app.state._mcp_cm = session_manager.run()
            await app.state._mcp_cm.__aenter__()

            async def _mcp_asgi(scope, receive, send):  # noqa: ANN001
                await session_manager.handle_request(scope, receive, send)

            app.mount("/mcp", _mcp_asgi)

            # Reverse path: when the aggregate changes, tell connected clients.
            async def _notify_changed() -> None:
                with contextlib.suppress(Exception):
                    await server.request_context.session.send_tool_list_changed()

            hub.on_changed = _notify_changed
            logger.info("mcphub: MCP Hub mounted at /mcp")
        except ImportError:
            logger.warning("mcphub: mcp SDK absent — /mcp unmounted, /healthz still served")
        except Exception:
            logger.exception("mcphub: MCP mount failed — Hub disabled")

        app.state._refresh_task = asyncio.create_task(_periodic_refresh(hub, settings.mcp_hub_refresh_seconds))
        try:
            yield
        finally:
            task = app.state._refresh_task
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            if app.state._mcp_cm is not None:
                with contextlib.suppress(Exception):
                    await app.state._mcp_cm.__aexit__(None, None, None)
            await hub.close()

    app = FastAPI(
        title="DevAI MCP Hub",
        version="0.1.0",
        description="Registry-driven MCP multiplexer — one /mcp surface over many downstream MCPs.",
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def _identity_and_profile(request: Request, call_next):  # noqa: ANN001
        """Terminate caller identity and resolve their tool-surface profile.

        Fail-closed gate (audit CODE-6): when the caller has no verified
        principal and ``DEVAI_MCP_HUB_REQUIRE_AUTH`` is on, the request is
        rejected with 401 instead of silently falling back to the default
        ToolProfile — an unauthenticated caller must not reach the mutating
        ``scm_*`` surface. The flag DEFAULTS OFF to stay behavior-neutral with
        the running system (auth is enabled deliberately in prod chart values);
        when off, an anonymous caller still resolves to the curated default
        profile exactly as before.
        """
        from devai.identity import extract_principal

        try:
            principal = await extract_principal(request)
        except Exception:  # noqa: BLE001 — never block a request on identity parse
            principal = None

        # /healthz must stay reachable for liveness/readiness probes even when
        # auth is required (probes carry no principal).
        require_auth = getattr(settings, "mcp_hub_require_auth", False)
        if require_auth and principal is None and not request.url.path.startswith("/healthz"):
            logger.warning("mcphub: rejecting unauthenticated request to %s (require_auth on)", request.url.path)
            return JSONResponse(
                {"error": "unauthorized", "detail": "mcp hub requires an authenticated principal"},
                status_code=401,
            )

        requested = request.query_params.get("profile", "")
        set_current_profile(profile_for_principal(principal, requested))
        # Per-user MCP federation keys on the caller's email (their own
        # connected servers); blank for anonymous/service callers.
        set_current_email(principal.email if principal else "")
        return await call_next(request)

    @app.get("/healthz")
    async def healthz():  # noqa: ANN202
        hub: MCPHub | None = getattr(app.state, "hub", None)
        if hub is None:
            return JSONResponse({"status": "ok", "service": "devai-mcp-hub", "hub": "disabled"})
        return JSONResponse({"status": "ok", "service": "devai-mcp-hub", **hub.status()})

    return app
