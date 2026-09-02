"""Core data model + namespacing for the DevAI MCP Hub.

The Hub is one MCP server to clients and an MCP *client* to each downstream
(docs/agentic/MCP-HUB.md §6). To present many small MCPs as one surface without
name collisions, every federated primitive is **namespaced** by its owning
server: a downstream tool ``security_scan_sast`` on server ``analyst-mcp`` is
exposed as ``analyst-mcp__security_scan_sast``. The mapping must be **reversible**
(route a namespaced call back to ``(server, wire-name)``) and **collision-free**
(two servers can each have a ``deploy`` tool).

This module is pure (no MCP SDK, no I/O) so the naming/routing contract is
fully unit-testable on its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Separator between the server segment and the downstream wire name. Two
# underscores: MCP tool names are `[a-zA-Z0-9_-]`, and downstream tools commonly
# contain single underscores, so a double underscore is an unambiguous, legal
# delimiter (the convention most MCP proxies converge on).
SEP = "__"


class RouteError(ValueError):
    """A namespaced name could not be split into (server, wire-name)."""


def namespaced(server: str, wire_name: str) -> str:
    """Build the client-facing name for a downstream primitive.

    >>> namespaced("analyst-mcp", "security_scan_sast")
    'analyst-mcp__security_scan_sast'
    """
    if not server or not wire_name:
        raise RouteError(f"namespaced: empty segment server={server!r} wire={wire_name!r}")
    if SEP in server:
        # A server name containing the separator would make routing ambiguous.
        raise RouteError(f"namespaced: server {server!r} contains reserved {SEP!r}")
    return f"{server}{SEP}{wire_name}"


def route(name: str) -> tuple[str, str]:
    """Split a client-facing namespaced name back to ``(server, wire_name)``.

    Splits on the FIRST separator only, so a downstream wire name may itself
    contain ``__`` and round-trips intact.

    >>> route("analyst-mcp__security_scan_sast")
    ('analyst-mcp', 'security_scan_sast')
    """
    server, sep, wire = name.partition(SEP)
    if not sep or not server or not wire:
        raise RouteError(f"route: {name!r} is not a namespaced hub name")
    return server, wire


# Resource URIs are namespaced by scheme rewrite instead of a name prefix, so a
# downstream ``file:///x`` becomes ``mcp+<server>:///x`` and round-trips back.
URI_SCHEME_PREFIX = "mcp+"


def namespaced_uri(server: str, uri: str) -> str:
    """Prefix a downstream resource URI's scheme with ``mcp+<server>+``.

    >>> namespaced_uri("analyst-mcp", "file:///report.json")
    'mcp+analyst-mcp+file:///report.json'
    """
    if not server:
        raise RouteError("namespaced_uri: empty server")
    return f"{URI_SCHEME_PREFIX}{server}+{uri}"


def route_uri(uri: str) -> tuple[str, str]:
    """Reverse :func:`namespaced_uri` → ``(server, original_uri)``."""
    if not uri.startswith(URI_SCHEME_PREFIX):
        raise RouteError(f"route_uri: {uri!r} is not a hub resource URI")
    rest = uri[len(URI_SCHEME_PREFIX) :]
    server, sep, original = rest.partition("+")
    if not sep or not server or not original:
        raise RouteError(f"route_uri: malformed hub URI {uri!r}")
    return server, original


# Health states for a downstream leg. ``ready`` is in the aggregate; ``degraded``
# / ``unreachable`` are dropped from the next listed surface but never
# fail the whole Hub (docs §6.5 degradation).
HEALTH_READY = "ready"
HEALTH_DEGRADED = "degraded"
HEALTH_UNREACHABLE = "unreachable"
HEALTH_UNKNOWN = "unknown"


@dataclass(slots=True)
class DownstreamSpec:
    """A downstream MCP the Hub federates, resolved from a ``kind:MCPServer``.

    ``auth_mode`` decides how the Hub injects credentials toward the downstream
    (the caller never holds them): ``jwt`` → a service token, ``header`` → static
    ``headers`` from the spec, ``mtls`` → client cert (handled by the transport),
    ``none`` → nothing.
    """

    name: str
    endpoint: str
    transport: str = "streamable-http"  # streamable-http | sse | stdio
    auth_mode: str = "none"  # jwt | none | header | mtls
    labels: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    health: str = HEALTH_UNKNOWN

    def is_servable(self) -> bool:
        """A spec the Hub can actually connect to over a network transport."""
        return (
            bool(self.name)
            and bool(self.endpoint)
            and self.transport
            in (
                "streamable-http",
                "sse",
            )
        )


@dataclass(slots=True)
class FederatedTool:
    """One downstream tool as the Hub presents it.

    ``name`` is the client-facing namespaced name; ``server``/``wire_name`` are
    the routing target. ``tier``/``labels`` come from the registry ``kind:Tool``
    artifact (when one exists) and drive per-caller budgeting (§6.5).
    """

    name: str  # namespaced, client-facing
    server: str
    wire_name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)
    tier: str = "core"  # core | extended | experimental
    labels: dict[str, str] = field(default_factory=dict)

    @classmethod
    def build(
        cls,
        server: str,
        wire_name: str,
        *,
        description: str = "",
        input_schema: dict[str, Any] | None = None,
        tier: str = "core",
        labels: dict[str, str] | None = None,
    ) -> FederatedTool:
        return cls(
            name=namespaced(server, wire_name),
            server=server,
            wire_name=wire_name,
            description=description,
            input_schema=input_schema or {},
            tier=tier,
            labels=labels or {},
        )


@dataclass(slots=True)
class FederatedPrompt:
    """A downstream prompt, namespaced like a tool."""

    name: str
    server: str
    wire_name: str
    description: str = ""
    arguments: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def build(
        cls, server: str, wire_name: str, *, description: str = "", arguments: list[dict[str, Any]] | None = None
    ) -> FederatedPrompt:
        return cls(
            name=namespaced(server, wire_name),
            server=server,
            wire_name=wire_name,
            description=description,
            arguments=arguments or [],
        )


@dataclass(slots=True)
class FederatedResource:
    """A downstream resource, namespaced by URI scheme rewrite."""

    uri: str  # namespaced, client-facing
    server: str
    wire_uri: str
    name: str = ""
    description: str = ""
    mime_type: str = ""

    @classmethod
    def build(
        cls, server: str, wire_uri: str, *, name: str = "", description: str = "", mime_type: str = ""
    ) -> FederatedResource:
        return cls(
            uri=namespaced_uri(server, wire_uri),
            server=server,
            wire_uri=wire_uri,
            name=name,
            description=description,
            mime_type=mime_type,
        )
