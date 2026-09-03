"""A single downstream MCP connection (the MCP SDK glue).

Each :class:`DownstreamConnection` is the Hub acting as an MCP *client* to one
downstream server (docs/agentic/MCP-HUB.md §6.2): it uses stateless
``server/discover``, records the downstream's capabilities, and forwards
list/call/get/read for every primitive. Transport connections may be pooled for
efficiency, but protocol correctness does not depend on connection affinity.

The ``mcp`` SDK is imported **lazily** inside :meth:`connect` so importing this
module — and the rest of the Hub's pure logic + tests — never requires the SDK
(the same adapter discipline the rest of the codebase follows).

Failure policy (docs §6.5 degradation): any downstream error marks the leg
``degraded`` and raises :class:`DownstreamError`; the Hub drops the leg from the
aggregate and keeps serving the rest. One bad mux leg never fails the Hub.
"""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack, suppress
from typing import Any

from devai.mcphub.model import (
    HEALTH_DEGRADED,
    HEALTH_READY,
    HEALTH_UNREACHABLE,
    DownstreamSpec,
)

logger = logging.getLogger(__name__)


class DownstreamError(RuntimeError):
    """A downstream MCP call failed; the leg should be treated as degraded."""


class DownstreamConnection:
    """MCP client connection to one downstream server."""

    def __init__(self, spec: DownstreamSpec, *, headers: dict[str, str] | None = None, timeout: float = 30.0):
        self.spec = spec
        self._headers = headers or {}
        self._timeout = timeout
        self._stack: AsyncExitStack | None = None
        self._session: Any = None
        self.capabilities: dict[str, Any] = {}

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def healthy(self) -> bool:
        return self.spec.health == HEALTH_READY and self._session is not None

    # -- lifecycle ----------------------------------------------------------

    async def connect(self) -> None:
        """Open the transport and discover the stateless server surface.

        Sets ``spec.health`` to ``ready`` on success, ``unreachable`` on
        failure. Raises :class:`DownstreamError` on failure so the Hub can drop
        the leg; the rest of the aggregate is unaffected.
        """
        stack = AsyncExitStack()
        try:
            read, write = await self._open_transport(stack)
            from mcp import ClientSession  # lazy

            session = await stack.enter_async_context(ClientSession(read, write))
            discovery = await session.discover()
            self._session = session
            self._stack = stack
            self.capabilities = self._extract_capabilities(discovery)
            self.spec.health = HEALTH_READY
            logger.info("mcphub: connected downstream %r (caps=%s)", self.name, sorted(self.capabilities))
        except Exception as e:  # noqa: BLE001 — normalize every failure
            self.spec.health = HEALTH_UNREACHABLE
            with suppress(Exception):
                await stack.aclose()
            raise DownstreamError(f"connect {self.name}: {e}") from e

    def _guard_endpoint(self) -> None:
        """Last-line SSRF guard at the dial site (defense in depth, audit CODE-6).

        Discovery already screens endpoints, but the actual network dial
        re-validates the registry-supplied endpoint — including a full DNS
        resolution that closes the hostname→private/metadata-IP rebinding gap — so
        no code path (a direct ``DownstreamConnection`` construction, a future
        caller that skips discovery) can dial a non-routable/off-allowlist host.

        BEHAVIOR-NEUTRALITY: enforcement is gated by ``settings.mcp_hub_ssrf_enforce``
        (default OFF). When OFF the guard runs in audit mode — a failing endpoint
        is logged but still dialed (today's behavior preserved). When ON, the
        bearer is stripped and the dial is refused (``DownstreamError`` → the Hub
        drops the leg, fail-closed). Settings are read lazily so this module stays
        import-pure for unit tests.
        """
        from devai.config import settings
        from devai.mcphub.discovery import EndpointGuardError, check_endpoint_url

        enforce = bool(getattr(settings, "mcp_hub_ssrf_enforce", False))
        try:
            check_endpoint_url(self.spec.endpoint, list(settings.a2a_allowed_url_suffixes), resolve=enforce)
        except EndpointGuardError as e:
            if enforce:
                self._headers.pop("Authorization", None)
                raise DownstreamError(f"connect {self.name}: {e}") from e
            logger.warning(
                "mcphub: downstream %r endpoint failed the SSRF guard (audit mode — dialing anyway; "
                "set DEVAI_MCP_HUB_SSRF_ENFORCE=true to block): %s",
                self.name,
                e,
            )

    async def _open_transport(self, stack: AsyncExitStack) -> tuple[Any, Any]:
        """Open the configured transport, returning (read, write) streams."""
        self._guard_endpoint()
        if self.spec.transport == "sse":
            from mcp.client.sse import sse_client  # lazy

            ctx = sse_client(self.spec.endpoint, headers=self._headers)
        else:  # streamable-http (default)
            import httpx2  # lazy
            from mcp.client.streamable_http import streamable_http_client  # lazy

            client = await stack.enter_async_context(
                httpx2.AsyncClient(
                    headers=self._headers,
                    timeout=self._timeout,
                    follow_redirects=False,
                    trust_env=False,
                )
            )
            ctx = streamable_http_client(self.spec.endpoint, http_client=client, terminate_on_close=False)
        streams = await stack.enter_async_context(ctx)
        # streamable-http yields (read, write, get_session_id); sse yields (read, write).
        read, write = streams[0], streams[1]
        return read, write

    async def close(self) -> None:
        if self._stack is not None:
            with suppress(Exception):
                await self._stack.aclose()
        self._stack = None
        self._session = None

    # -- primitives ---------------------------------------------------------

    async def list_tools(self) -> list[dict[str, Any]]:
        """Wire-level tool list: ``[{name, description, inputSchema}, …]``."""
        res = await self._guard("list_tools", lambda s: s.list_tools())
        return [
            {
                "name": t.name,
                "description": getattr(t, "description", "") or "",
                "inputSchema": getattr(t, "inputSchema", None) or {},
            }
            for t in getattr(res, "tools", []) or []
        ]

    async def call_tool(self, wire_name: str, arguments: dict[str, Any]) -> Any:
        return await self._guard("call_tool", lambda s: s.call_tool(wire_name, arguments or {}))

    async def list_prompts(self) -> list[dict[str, Any]]:
        if not self.capabilities.get("prompts"):
            return []
        res = await self._guard("list_prompts", lambda s: s.list_prompts())
        return [
            {
                "name": p.name,
                "description": getattr(p, "description", "") or "",
                "arguments": [self._arg(a) for a in getattr(p, "arguments", []) or []],
            }
            for p in getattr(res, "prompts", []) or []
        ]

    async def get_prompt(self, wire_name: str, arguments: dict[str, Any]) -> Any:
        return await self._guard("get_prompt", lambda s: s.get_prompt(wire_name, arguments or {}))

    async def list_resources(self) -> list[dict[str, Any]]:
        if not self.capabilities.get("resources"):
            return []
        res = await self._guard("list_resources", lambda s: s.list_resources())
        return [
            {
                "uri": str(r.uri),
                "name": getattr(r, "name", "") or "",
                "description": getattr(r, "description", "") or "",
                "mimeType": getattr(r, "mimeType", "") or "",
            }
            for r in getattr(res, "resources", []) or []
        ]

    async def read_resource(self, wire_uri: str) -> Any:
        return await self._guard("read_resource", lambda s: s.read_resource(wire_uri))

    # -- internals ----------------------------------------------------------

    async def _guard(self, op: str, fn: Any) -> Any:
        """Run a session call, normalizing failure to a degraded leg + error."""
        if self._session is None:
            raise DownstreamError(f"{self.name}.{op}: not connected")
        try:
            return await fn(self._session)
        except Exception as e:  # noqa: BLE001
            self.spec.health = HEALTH_DEGRADED
            logger.warning("mcphub: downstream %r %s failed: %s", self.name, op, e)
            raise DownstreamError(f"{self.name}.{op}: {e}") from e

    @staticmethod
    def _arg(a: Any) -> dict[str, Any]:
        return {
            "name": getattr(a, "name", "") or "",
            "description": getattr(a, "description", "") or "",
            "required": bool(getattr(a, "required", False)),
        }

    @staticmethod
    def _extract_capabilities(discovery: Any) -> dict[str, Any]:
        """Project ``server/discover`` capabilities to a plain dict.

        The Hub advertises the union of what it can proxy; here we record what
        THIS leg supports so we don't call ``prompts/list`` on a server that
        doesn't have prompts.
        """
        caps = getattr(discovery, "capabilities", None)
        if caps is None:
            return {}
        out: dict[str, Any] = {}
        for key in ("tools", "prompts", "resources", "logging", "completions"):
            val = getattr(caps, key, None)
            if val is not None:
                out[key] = True
        return out
