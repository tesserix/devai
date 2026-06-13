"""devai-mcp-bridge — stdio MCP servers served over streamable-https.

The adapter that lets the hub federate the npx/stdio long tail (draw.io,
filesystem, Postgres, Slack, …) without anything speaking stdio outside this
pod. See app.create_bridge_app and runner.open_stdio_session.
"""

from devai.mcpbridge.app import create_bridge_app, load_catalog_specs
from devai.mcpbridge.runner import LaunchSpec, command_allowed

__all__ = ["LaunchSpec", "command_allowed", "create_bridge_app", "load_catalog_specs"]
