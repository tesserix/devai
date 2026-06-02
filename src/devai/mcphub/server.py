"""The Hub's client-facing MCP server (one ``/mcp`` for everything).

Built on the **low-level** ``mcp.server.lowlevel.Server`` rather than FastMCP,
because the tool/prompt/resource lists are **dynamic** (resolved per caller from
the registry-driven aggregate) and calls must be **routed** to a downstream by
name — neither fits FastMCP's static decorator model.

Per-caller surface budgeting (docs/agentic/MCP-HUB.md §6.5): an ASGI middleware
terminates the caller's identity (the gateway's ``X-Forwarded-*`` headers, same
trust model as ``devai.identity``) and stashes a resolved :class:`ToolProfile`
in a context var; the ``tools/list`` handler reads it so each caller sees only
their scoped subset.

The ``mcp`` SDK is imported lazily inside :func:`build_hub_server` so the pure
profile-resolution logic here imports and tests without the SDK.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from devai.mcphub.profile import ToolProfile

if TYPE_CHECKING:
    from devai.identity import Principal
    from devai.mcphub.hub import MCPHub

logger = logging.getLogger(__name__)

# Per-request caller profile, set by the ASGI middleware before the MCP handler
# runs. Default is None (a ContextVar default must be immutable); an unset caller
# resolves to the curated `core` surface in current_profile().
_CURRENT_PROFILE: ContextVar[ToolProfile | None] = ContextVar("mcphub_profile", default=None)


def set_current_profile(profile: ToolProfile) -> None:
    _CURRENT_PROFILE.set(profile)


def current_profile() -> ToolProfile:
    return _CURRENT_PROFILE.get() or ToolProfile.default()


# Roles/emails that get the full (still budget-capped) surface. Kept tiny and
# explicit; a real deployment resolves this from the ToolProfile artifacts (§9.3).
_ADMIN_ROLES = frozenset({"admin", "platform-admin"})


def profile_for_principal(principal: Principal | None, requested: str = "") -> ToolProfile:
    """Resolve the tool-surface profile for a caller.

    Precedence: an explicit ``?profile=unrestricted`` from an admin → the full
    surface; an admin by role → unrestricted; everyone else → the default
    ``core`` surface. This is the seam where ``ToolProfile`` *artifacts* plug in
    (Phase 5) — for now it's a safe, explicit default.
    """
    is_admin = bool(principal and _ADMIN_ROLES.intersection(principal.roles))
    if requested == "unrestricted" and is_admin:
        return ToolProfile.unrestricted()
    if is_admin:
        return ToolProfile.unrestricted()
    return ToolProfile.default()


def build_hub_server(hub: MCPHub) -> Any:
    """Wire a low-level MCP ``Server`` whose handlers delegate to ``hub``.

    Returns the ``Server`` instance (the caller mounts it over Streamable HTTP).
    Raises ``ImportError`` if the ``mcp`` SDK is absent — the app wiring catches
    it and leaves the Hub unmounted, exactly like the existing ``/mcp`` channel.
    """
    import mcp.types as t  # lazy
    from mcp.server.lowlevel import Server  # lazy

    server: Any = Server("devai-mcp-hub")

    @server.list_tools()
    async def _list_tools() -> list[Any]:
        budget = hub.list_tools(current_profile())
        if budget.truncated:
            logger.info(
                "mcphub: served %d tools (%d cut to budget)", len(budget.selected), len(budget.dropped_by_budget)
            )
        return [
            t.Tool(name=ft.name, description=ft.description, inputSchema=ft.input_schema or {"type": "object"})
            for ft in budget.selected
        ]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any]) -> Any:
        result = await hub.call_tool(name, arguments)
        # Pass the downstream's content blocks straight through; if a leg returns
        # a bare value, wrap it so the client always gets valid content.
        content = getattr(result, "content", None)
        if content is not None:
            return content
        return [t.TextContent(type="text", text=str(result))]

    @server.list_prompts()
    async def _list_prompts() -> list[Any]:
        return [
            t.Prompt(
                name=fp.name,
                description=fp.description,
                arguments=[
                    t.PromptArgument(
                        name=a.get("name", ""), description=a.get("description", ""), required=a.get("required", False)
                    )
                    for a in fp.arguments
                ],
            )
            for fp in hub.list_prompts()
        ]

    @server.get_prompt()
    async def _get_prompt(name: str, arguments: dict[str, Any] | None) -> Any:
        return await hub.get_prompt(name, arguments or {})

    @server.list_resources()
    async def _list_resources() -> list[Any]:
        return [
            t.Resource(uri=fr.uri, name=fr.name or fr.uri, description=fr.description, mimeType=fr.mime_type or None)
            for fr in hub.list_resources()
        ]

    @server.read_resource()
    async def _read_resource(uri: Any) -> Any:
        return await hub.read_resource(str(uri))

    return server
