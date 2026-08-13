"""The sandbox workspace as an MCP leg.

A sandbox's shell, files and (later) browser reach agents the way every other
capability does — namespaced tools on the Hub — rather than through a bespoke
client. Going through the Hub means the workspace inherits what the Hub already
enforces: per-caller budgeting from ``kind:Tool`` metadata and the sandbox tool
gateway's real/mock/replay/block modes.

The leg exists only inside the sandbox it belongs to: it is resolved from the
pod's own env, so the same tool name outside that pod resolves to nothing.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from devai.mcphub.model import FederatedTool, route
from devai.sandbox.workspace_client import WorkspaceClient

logger = logging.getLogger(__name__)

SANDBOX_SERVER = "sandbox"

_STRING = {"type": "string"}

_TOOLS: dict[str, tuple[str, dict[str, Any]]] = {
    "shell_exec": (
        "Run a shell command in the sandbox workspace and return its output and exit code.",
        {
            "type": "object",
            "properties": {"command": _STRING, "timeout": {"type": "number"}},
            "required": ["command"],
        },
    ),
    "file_read": (
        "Read a file from the sandbox workspace.",
        {"type": "object", "properties": {"path": _STRING}, "required": ["path"]},
    ),
    "file_write": (
        "Write a file in the sandbox workspace.",
        {"type": "object", "properties": {"path": _STRING, "content": _STRING}, "required": ["path", "content"]},
    ),
    "file_list": (
        "List the entries under a path in the sandbox workspace.",
        {"type": "object", "properties": {"path": _STRING}, "required": []},
    ),
    "file_search": (
        "Search the sandbox workspace for a literal string.",
        {"type": "object", "properties": {"needle": _STRING, "path": _STRING}, "required": ["needle"]},
    ),
}


class WorkspaceLeg:
    """Federates one sandbox's workspace tools, for that sandbox only."""

    def __init__(self, *, client: Any) -> None:
        self._client = client
        self._healthy = True

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> WorkspaceLeg | None:
        """Build the leg this pod's own sandbox exposes, or None.

        None outside a sandbox, and none for a sandbox with no workspace — the
        Hub then has no ``sandbox__*`` tools at all rather than tools that fail.
        """
        endpoint = (env.get("DEVAI_SANDBOX_WORKSPACE") or "").strip()
        token = (env.get("DEVAI_SANDBOX_WORKSPACE_TOKEN") or "").strip()
        if not env.get("DEVAI_SANDBOX_ID") or not endpoint or not token:
            return None
        return cls(client=WorkspaceClient(endpoint, token=token))

    def tools(self) -> list[FederatedTool]:
        if not self._healthy:
            return []
        return [
            FederatedTool.build(
                SANDBOX_SERVER,
                wire,
                description=description,
                input_schema=schema,
                labels={"mcp.devai.io/server": SANDBOX_SERVER, "devai.io/tier": "core"},
            )
            for wire, (description, schema) in _TOOLS.items()
        ]

    def owns(self, name: str) -> bool:
        return name.startswith(f"{SANDBOX_SERVER}__")

    async def probe(self) -> bool:
        """Whether the workspace still answers — a reaped one leaves no stale tools."""
        try:
            await self._client.list(".")
        except Exception as e:  # noqa: BLE001 — an unreachable workspace is a dropped leg, not a crash
            logger.info("mcphub: sandbox workspace leg dropped — %s", e)
            self._healthy = False
        else:
            self._healthy = True
        return self._healthy

    async def call(self, name: str, arguments: dict[str, Any]) -> Any:
        _, wire = route(name)
        args = arguments or {}
        if wire == "shell_exec":
            return await self._client.exec(str(args.get("command", "")), timeout=float(args.get("timeout", 120.0)))
        if wire == "file_read":
            return await self._client.read(str(args.get("path", "")))
        if wire == "file_write":
            return await self._client.write(str(args.get("path", "")), str(args.get("content", "")))
        if wire == "file_list":
            return await self._client.list(str(args.get("path", ".")))
        if wire == "file_search":
            return await self._client.search(str(args.get("needle", "")), str(args.get("path", ".")))
        raise ValueError(f"mcphub: {name!r} is not a sandbox workspace tool ({wire!r})")


__all__ = ["SANDBOX_SERVER", "WorkspaceLeg"]
