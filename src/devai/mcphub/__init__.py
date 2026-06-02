"""DevAI MCP Hub — registry-driven MCP multiplexing.

One ``/mcp`` surface that federates every registry-declared downstream MCP server
(docs/agentic/MCP-HUB.md). Public surface:

  - :func:`create_hub_app` — the ``devai-mcp-hub`` ASGI app.
  - :class:`MCPHub` — the multiplexer (discovery + federation + routing).
  - :class:`ToolProfile` — per-caller surface budgeting.

The pure pieces (model/naming, discovery, profile) import without the ``mcp``
SDK; the SDK is lazy-loaded only when the Hub actually mounts/connects.
"""

from __future__ import annotations

from devai.mcphub.hub import MCPHub
from devai.mcphub.model import DownstreamSpec, FederatedTool, namespaced, route
from devai.mcphub.profile import ToolProfile, select


def create_hub_app():  # noqa: ANN201 — FastAPI imported lazily inside
    """Entry point for ``devai-mcp-hub`` (see pyproject [project.scripts])."""
    from devai.mcphub.app import create_hub_app as _factory

    return _factory()


__all__ = [
    "MCPHub",
    "DownstreamSpec",
    "FederatedTool",
    "ToolProfile",
    "namespaced",
    "route",
    "select",
    "create_hub_app",
]
