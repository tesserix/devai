"""The DevAI MCP Hub — registry-driven 1↔N multiplexer.

One MCP server to clients, an MCP client to N downstreams (docs/agentic/
MCP-HUB.md §6). :class:`MCPHub` owns:

  - **Discovery** — reads ``kind:MCPServer`` from the registry (never hardcoded).
  - **Federation** — connects to each downstream, lists every primitive, and
    namespaces them (``server__tool``) into one collision-free surface.
  - **Enrichment** — overlays each tool with its registry ``kind:Tool`` metadata
    (tier/labels) so per-caller budgeting (§6.5) is meaningful.
  - **Routing** — a ``tools/call`` for ``analyst__X`` goes to the ``analyst`` leg.
  - **Degradation** — an unreachable/degraded leg is dropped from the aggregate
    (a ``list_changed`` fires); the rest keep serving. One bad leg never fails
    the Hub.

Aggregation runs all legs in parallel with per-leg isolation: a slow or broken
downstream can't stall or sink the surface.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from devai.mcphub.discovery import discover, downstream_headers
from devai.mcphub.downstream import DownstreamConnection, DownstreamError
from devai.mcphub.model import (
    FederatedPrompt,
    FederatedResource,
    FederatedTool,
    route,
    route_uri,
)
from devai.mcphub.personal import PersonalLegs
from devai.mcphub.profile import BudgetResult, ToolProfile, select
from devai.mcphub.sandbox_leg import WorkspaceLeg
from devai.sandbox.gateway import ToolGateway, guard_mcp_call

if TYPE_CHECKING:
    from devai.registry.client import RegistryClient

logger = logging.getLogger(__name__)

# Registry label/annotation keys (mirror agentic-registry conventions, docs §4.3).
LABEL_SERVER = "mcp.devai.io/server"
LABEL_TIER = "devai.io/tier"
ANNO_WIRE_NAME = "mcp.devai.io/wire-name"


class MCPHub:
    """Stateful federation of all registry-declared downstream MCP servers."""

    def __init__(
        self,
        registry: RegistryClient,
        *,
        service_token: str = "",
        connect_timeout: float = 15.0,
        max_concurrency: int = 16,
    ):
        self._registry = registry
        self._service_token = service_token
        self._connect_timeout = connect_timeout
        self._sem = asyncio.Semaphore(max(1, max_concurrency))
        self._lock = asyncio.Lock()

        self._connections: dict[str, DownstreamConnection] = {}
        self._tools: dict[str, FederatedTool] = {}  # namespaced name -> tool
        self._prompts: dict[str, FederatedPrompt] = {}
        self._resources: dict[str, FederatedResource] = {}  # namespaced uri -> resource

        # Per-caller federation of the user's OWN connected MCP servers
        # (Settings → MCP Server). Resolved + connected on demand, scoped to
        # the calling principal, never shared across users.
        self.personal = PersonalLegs(connect_timeout=connect_timeout)

        # Reverse path: set by the server to emit a single tools/list_changed to
        # clients when the aggregate changes (registry/cache change, leg drop).
        self.on_changed: Callable[[], Awaitable[None]] | None = None

        # None outside a sandbox — the hub then behaves exactly as before.
        self._gateway = ToolGateway.from_env(os.environ)

        # The workspace of the sandbox this process runs in, federated like any
        # other leg. Resolved from the pod's own env, so a sandbox's tools are
        # never reachable from outside it.
        self.sandbox = WorkspaceLeg.from_env(os.environ)

    # -- lifecycle ----------------------------------------------------------

    async def refresh(self) -> None:
        """Re-discover downstreams, (re)connect, and rebuild the aggregate.

        Idempotent and safe to call on a timer or a registry-change signal.
        Holds a lock so concurrent refreshes don't interleave the connection
        table. Always finishes with a list_changed notification if the surface
        moved.
        """
        async with self._lock:
            specs = discover(self._registry)
            wanted = {s.name: s for s in specs}

            # Drop legs no longer in the registry.
            for name in list(self._connections):
                if name not in wanted:
                    await self._drop(name)

            # Connect new / not-yet-connected legs in parallel, isolated.
            await asyncio.gather(
                *(self._ensure_connected(spec) for spec in specs),
                return_exceptions=True,
            )

            catalog = self._registry_tool_catalog()
            await self._rebuild_aggregate(catalog)
            if self.sandbox is not None:
                # A reaped sandbox must leave no stale tools in the aggregate.
                await self.sandbox.probe()

        if self.on_changed is not None:
            with suppress(Exception):
                await self.on_changed()

    async def close(self) -> None:
        async with self._lock:
            for name in list(self._connections):
                await self._drop(name)
            self._tools.clear()
            self._prompts.clear()
            self._resources.clear()
        with suppress(Exception):
            await self.personal.close()

    async def _ensure_connected(self, spec: Any) -> None:
        existing = self._connections.get(spec.name)
        if existing is not None and existing.healthy:
            # Refresh endpoint/auth in place — but PRESERVE the live health.
            # The freshly-discovered spec carries the registry default
            # ("unknown"), and `healthy` reads spec.health: adopting it
            # verbatim silently flipped every connected leg to unhealthy one
            # refresh after it connected, so tools/call answered "downstream
            # unavailable" while the session underneath was fine.
            spec.health = existing.spec.health
            existing.spec = spec
            return
        if existing is not None:
            await self._drop(spec.name)
        headers = downstream_headers(spec, service_token=self._service_token)
        conn = DownstreamConnection(spec, headers=headers, timeout=self._connect_timeout)
        async with self._sem:
            try:
                await asyncio.wait_for(conn.connect(), timeout=self._connect_timeout)
            except Exception as e:  # noqa: BLE001 — any failure → drop this leg only
                logger.warning("mcphub: downstream %r unavailable, dropped from aggregate: %s", spec.name, e)
                return
        self._connections[spec.name] = conn

    async def _drop(self, name: str) -> None:
        conn = self._connections.pop(name, None)
        if conn is not None:
            await conn.close()

    # -- aggregation --------------------------------------------------------

    async def _rebuild_aggregate(self, catalog: dict[tuple[str, str], dict[str, Any]]) -> None:
        """List every primitive from every healthy leg, namespaced + enriched."""
        tools: dict[str, FederatedTool] = {}
        prompts: dict[str, FederatedPrompt] = {}
        resources: dict[str, FederatedResource] = {}

        async def gather_one(conn: DownstreamConnection) -> None:
            try:
                wire_tools = await conn.list_tools()
            except DownstreamError:
                return  # leg already marked degraded; skip it
            for wt in wire_tools:
                meta = catalog.get((conn.name, wt["name"]), {})
                ft = FederatedTool.build(
                    conn.name,
                    wt["name"],
                    description=wt.get("description", "") or str(meta.get("description", "")),
                    input_schema=wt.get("inputSchema") or {},
                    tier=str(meta.get("tier", "core")),
                    labels=meta.get("labels", {}),
                )
                tools[ft.name] = ft
            for wp in await _safe(conn.list_prompts()):
                fp = FederatedPrompt.build(
                    conn.name, wp["name"], description=wp.get("description", ""), arguments=wp.get("arguments", [])
                )
                prompts[fp.name] = fp
            for wr in await _safe(conn.list_resources()):
                fr = FederatedResource.build(
                    conn.name,
                    wr["uri"],
                    name=wr.get("name", ""),
                    description=wr.get("description", ""),
                    mime_type=wr.get("mimeType", ""),
                )
                resources[fr.uri] = fr

        healthy = [c for c in self._connections.values() if c.healthy]
        await asyncio.gather(*(gather_one(c) for c in healthy), return_exceptions=True)

        self._tools, self._prompts, self._resources = tools, prompts, resources
        logger.info(
            "mcphub: aggregate rebuilt — %d tools, %d prompts, %d resources across %d leg(s)",
            len(tools),
            len(prompts),
            len(resources),
            len(healthy),
        )

    def _registry_tool_catalog(self) -> dict[tuple[str, str], dict[str, Any]]:
        """Map ``(server, wire_name)`` → ``{tier, labels, description}`` from the
        registry's ``kind:Tool`` artifacts, for enrichment/budgeting.

        Reads ``metadata.labels[mcp.devai.io/server]`` + ``[devai.io/tier]`` and
        ``metadata.annotations[mcp.devai.io/wire-name]`` (falling back to the
        spec/metadata name). Best-effort; an empty catalog just means every tool
        defaults to ``tier: core``.
        """
        out: dict[tuple[str, str], dict[str, Any]] = {}
        try:
            artifacts = self._registry.list_tool_artifacts()
        except Exception:  # noqa: BLE001
            return out
        for a in artifacts:
            if not isinstance(a, dict):
                continue
            meta = a.get("metadata") or {}
            spec = a.get("spec") or {}
            labels = meta.get("labels") or {}
            annos = meta.get("annotations") or {}
            server = labels.get(LABEL_SERVER, "")
            wire = annos.get(ANNO_WIRE_NAME) or spec.get("wireName") or meta.get("name", "")
            if not server or not wire:
                continue
            out[(server, wire)] = {
                "tier": labels.get(LABEL_TIER, "core") or "core",
                "labels": {str(k): str(v) for k, v in labels.items()},
                "description": spec.get("description", "") or "",
            }
        return out

    # -- client-facing surface ---------------------------------------------

    def list_tools(self, profile: ToolProfile | None = None) -> BudgetResult:
        """Return the caller's budgeted, namespaced tool surface (§6.5)."""
        surface = list(self._tools.values())
        if self.sandbox is not None:
            surface += self.sandbox.tools()
        return select(surface, profile or ToolProfile.default())

    async def list_tools_for(self, identity: Any, profile: ToolProfile | None = None) -> BudgetResult:
        """The shared budgeted surface PLUS the caller's own MCP servers' tools.

        Personal tools (``usr-<instance>__<tool>``) are appended AFTER budget
        selection so a user always sees every tool of the servers they
        connected — they're never cut to the shared budget. Empty email →
        just the shared surface.
        """
        base = self.list_tools(profile)
        descriptors = await self.personal.tool_descriptors(identity) if identity else []
        if not descriptors:
            return base
        personal = [
            FederatedTool(
                name=d["name"],
                server=d["name"].split("__", 1)[0],
                wire_name=d["name"].split("__", 1)[-1],
                description=d.get("description", ""),
                input_schema=d.get("input_schema") or {"type": "object"},
                tier="core",
                labels={"devai.io/personal": "true"},
            )
            for d in descriptors
        ]
        return BudgetResult(
            selected=[*base.selected, *personal],
            dropped_by_filter=base.dropped_by_filter,
            dropped_by_budget=base.dropped_by_budget,
        )

    def list_prompts(self) -> list[FederatedPrompt]:
        return list(self._prompts.values())

    def list_resources(self) -> list[FederatedResource]:
        return list(self._resources.values())

    async def call_tool(self, name: str, arguments: dict[str, Any], *, identity: Any = None, email: str = "") -> Any:
        # In a sandbox every MCP tool obeys the same policy as a built-in one:
        # an unclassified downstream tool may well be `refund_customer`.
        return await guard_mcp_call(
            self._gateway,
            name,
            arguments or {},
            lambda: self._call_downstream(name, arguments, identity=identity or email),
        )

    async def _call_downstream(self, name: str, arguments: dict[str, Any], *, identity: Any = None) -> Any:
        # A personal-leg tool (usr-…__…) routes to the caller's OWN server —
        # only ever resolvable with the caller's email, so isolation holds.
        if self.sandbox is not None and self.sandbox.owns(name):
            return await self.sandbox.call(name, arguments or {})
        if self.personal.owns(name):
            if not identity:
                raise DownstreamError(f"mcphub: {name!r} is a personal MCP tool but the call carries no identity")
            return await self.personal.call(identity, name, arguments or {})
        server, wire = route(name)
        conn = self._healthy_leg(server, name)
        return await conn.call_tool(wire, arguments or {})

    async def get_prompt(self, name: str, arguments: dict[str, Any]) -> Any:
        server, wire = route(name)
        conn = self._healthy_leg(server, name)
        return await conn.get_prompt(wire, arguments or {})

    async def read_resource(self, uri: str) -> Any:
        server, wire_uri = route_uri(uri)
        conn = self._healthy_leg(server, uri)
        return await conn.read_resource(wire_uri)

    def _healthy_leg(self, server: str, ref: str) -> DownstreamConnection:
        conn = self._connections.get(server)
        if conn is None or not conn.healthy:
            # Degrade cleanly: an in-flight call to a dropped leg errors with a
            # clear message rather than hanging or 500-ing the whole Hub.
            raise DownstreamError(f"mcphub: downstream {server!r} for {ref!r} is unavailable")
        return conn

    # -- introspection (for /healthz, dashboard) ---------------------------

    def status(self) -> dict[str, Any]:
        return {
            "downstreams": [
                {"name": c.name, "health": c.spec.health, "transport": c.spec.transport}
                for c in self._connections.values()
            ],
            "toolCount": len(self._tools),
            "promptCount": len(self._prompts),
            "resourceCount": len(self._resources),
        }


async def _safe(coro: Awaitable[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Await a list-returning coroutine, returning [] on a degraded-leg error."""
    try:
        return await coro
    except DownstreamError:
        return []
