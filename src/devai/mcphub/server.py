"""The Hub's client-facing MCP server (one ``/mcp`` for everything).

Built on the **low-level** ``mcp.server.lowlevel.Server`` rather than MCPServer,
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

from devai.identity import Principal
from devai.mcphub.profile import ToolProfile

if TYPE_CHECKING:
    from devai.mcphub.hub import MCPHub

logger = logging.getLogger(__name__)

# Per-request caller profile, set by the ASGI middleware before the MCP handler
# runs. Default is None (a ContextVar default must be immutable); an unset caller
# resolves to the curated `core` surface in current_profile().
_CURRENT_PROFILE: ContextVar[ToolProfile | None] = ContextVar("mcphub_profile", default=None)

# The caller's email, set alongside the profile by the ASGI middleware. Drives
# per-user MCP federation (their own connected servers). "" = anonymous/system.
_CURRENT_PRINCIPAL: ContextVar[Principal | str | None] = ContextVar("mcphub_principal", default=None)


def set_current_profile(profile: ToolProfile) -> None:
    _CURRENT_PROFILE.set(profile)


def current_profile() -> ToolProfile:
    return _CURRENT_PROFILE.get() or ToolProfile.default()


def set_current_email(email: str) -> None:
    _CURRENT_PRINCIPAL.set(email or "")


def current_email() -> str:
    principal = _CURRENT_PRINCIPAL.get()
    return str(getattr(principal, "email", "") or principal or "")


def set_current_principal(principal: Principal | None) -> None:
    _CURRENT_PRINCIPAL.set(principal)


def current_principal() -> Principal | str | None:
    return _CURRENT_PRINCIPAL.get()


# Roles/emails that get the full (still budget-capped) surface. Kept tiny and
# explicit; a real deployment resolves this from the ToolProfile artifacts (§9.3).
_ADMIN_ROLES = frozenset({"admin", "platform-admin"})


def profile_for_principal(principal: Principal | None, requested: str = "") -> ToolProfile:
    """Resolve the tool-surface profile for a caller.

    Precedence: an explicit ``?profile=unrestricted`` from an admin → the full
    surface; an admin by role → unrestricted; everyone else → the default
    ``core`` surface. This is the seam where ``ToolProfile`` *artifacts* plug in
    (Phase 5) — for now it's a safe, explicit default.

    Auth note (audit CODE-6): the fail-closed gate that rejects an anonymous
    caller (so they never reach the mutating ``scm_*`` surface) lives upstream in
    the ASGI middleware (``app._identity_and_profile``), keyed on
    ``DEVAI_MCP_HUB_REQUIRE_AUTH``. By the time this resolver runs with
    ``principal is None``, that flag is off, so anonymous deliberately keeps the
    curated default surface (behavior-neutral). Admin (unrestricted) is only ever
    granted to a verified principal — never derived from a missing identity.
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

    async def _list_tools(_ctx: Any, _params: Any) -> Any:
        # Shared registry surface + the caller's OWN connected MCP servers.
        budget = await hub.list_tools_for(current_principal(), current_profile())
        if budget.truncated:
            logger.info(
                "mcphub: served %d tools (%d cut to budget)", len(budget.selected), len(budget.dropped_by_budget)
            )
        return t.ListToolsResult(
            tools=[
                t.Tool(name=ft.name, description=ft.description, input_schema=ft.input_schema or {"type": "object"})
                for ft in budget.selected
            ]
        )

    async def _call_tool(_ctx: Any, params: Any) -> Any:
        result = await hub.call_tool(params.name, params.arguments or {}, identity=current_principal())
        # Pass the downstream's content blocks straight through; if a leg returns
        # a bare value, wrap it so the client always gets valid content.
        content = getattr(result, "content", None)
        if content is not None:
            return t.CallToolResult(content=content, is_error=bool(getattr(result, "isError", False)))
        return t.CallToolResult(content=[t.TextContent(type="text", text=str(result))])

    async def _list_prompts(_ctx: Any, _params: Any) -> Any:
        return t.ListPromptsResult(
            prompts=[
                t.Prompt(
                    name=fp.name,
                    description=fp.description,
                    arguments=[
                        t.PromptArgument(
                            name=a.get("name", ""),
                            description=a.get("description", ""),
                            required=a.get("required", False),
                        )
                        for a in fp.arguments
                    ],
                )
                for fp in hub.list_prompts()
            ]
        )

    async def _get_prompt(_ctx: Any, params: Any) -> Any:
        return await hub.get_prompt(params.name, params.arguments or {})

    async def _list_resources(_ctx: Any, _params: Any) -> Any:
        return t.ListResourcesResult(
            resources=[
                t.Resource(
                    uri=fr.uri, name=fr.name or fr.uri, description=fr.description, mime_type=fr.mime_type or None
                )
                for fr in hub.list_resources()
            ]
        )

    async def _read_resource(_ctx: Any, params: Any) -> Any:
        return await hub.read_resource(str(params.uri))

    return Server(
        "devai-mcp-hub",
        on_list_tools=_list_tools,
        on_call_tool=_call_tool,
        on_list_prompts=_list_prompts,
        on_get_prompt=_get_prompt,
        on_list_resources=_list_resources,
        on_read_resource=_read_resource,
    )
