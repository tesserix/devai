"""stdio→streamable-http bridge core — spawn an MCP server, proxy its tools.

The MCP ecosystem's long tail (draw.io, filesystem, Postgres, Slack, Brave,
Playwright, …) ships as stdio servers run via ``npx``/``docker``. The DevAI
hub only dials streamable-https. This module is the adapter between the two:
given a catalog server's launch spec, it spawns the process, opens an MCP
stdio client to it, and re-exposes its primitives over a streamable-http MCP
server the hub federates like any other downstream.

Security model (everything streamable-https to the mesh):
  - Per-session subprocess: each client session spawns its OWN process, so
    one caller's server can't see another's state.
  - Secrets are request-scoped: a server that needs a credential (Postgres
    connection string, Slack token) receives it as the bridge request's
    ``x-mcp-secret`` header and the bridge substitutes it into the launch
    env — nothing is persisted in the bridge.
  - The launch spec is registry-supplied (the catalog seed), and only
    commands on the allowlist (``DEVAI_MCPBRIDGE_ALLOWED_COMMANDS``, default
    ``npx``) may run — a published catalog entry can't run an arbitrary binary.
  - The pod is sandboxed by the chart (non-root, read-only rootfs + tmp
    workspace, egress NetworkPolicy, CPU/mem limits).
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

_SECRET_PLACEHOLDER = "{secret}"


@dataclass(slots=True)
class LaunchSpec:
    """A stdio MCP server's launch recipe (from a catalog seed's ``spec.stdio``)."""

    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_spec(cls, stdio: dict[str, Any]) -> LaunchSpec:
        return cls(
            command=str(stdio.get("command", "")),
            args=[str(a) for a in (stdio.get("args") or [])],
            env={str(k): str(v) for k, v in (stdio.get("env") or {}).items()},
        )

    def resolve_env(self, *, secret: str = "", prefs: dict[str, str] | None = None) -> dict[str, str]:
        """Substitute ``{secret}`` and ``{prefs:key}`` placeholders in the env.

        ``{secret}`` → the request's x-mcp-secret value; ``{prefs:key}`` → a
        non-secret pref (e.g. a team id) the connector carries in the header.
        Unresolved placeholders are dropped (the server gets no value).
        """
        prefs = prefs or {}
        out: dict[str, str] = {}
        for key, tmpl in self.env.items():
            if tmpl == _SECRET_PLACEHOLDER:
                if secret:
                    out[key] = secret
            elif tmpl.startswith("{prefs:") and tmpl.endswith("}"):
                pref_key = tmpl[len("{prefs:") : -1]
                if prefs.get(pref_key):
                    out[key] = str(prefs[pref_key])
            else:
                out[key] = tmpl
        return out


def command_allowed(command: str, allowed: list[str]) -> bool:
    """True if ``command`` (basename) is on the allowlist. ``*`` allows all."""
    if "*" in allowed:
        return True
    base = command.rsplit("/", 1)[-1]
    return base in allowed


@asynccontextmanager
async def stdio_session(spec: LaunchSpec, env: dict[str, str], *, timeout: float = 60.0):
    """Spawn the stdio MCP server, discover it, then tear it down.

    MUST be entered and exited within the SAME task: the mcp SDK's stdio
    transport uses anyio task groups whose cancel scopes are task-bound, so a
    session opened in one request task and closed in another raises
    "Attempted to exit a cancel scope that isn't the current task's". The
    bridge therefore opens a fresh session per call (npx caches the package
    after the first spawn, so re-spawns are fast). Lazy-imports the mcp SDK.
    """
    import anyio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=spec.command, args=spec.args, env=env or None)
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        with anyio.fail_after(timeout):
            await session.discover()
        yield session


__all__ = ["LaunchSpec", "command_allowed", "stdio_session"]
