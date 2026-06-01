"""MCP channel — exposes DevAI AS an MCP server over remote (Streamable HTTP).

Any MCP-capable client/agent can reach the same conversational brain (and a few
action tools) at ``/mcp``. The transport is FastMCP's Streamable HTTP app,
mounted into the main FastAPI app so it shares the ingress + port.

This module has two parts:
  - ``McpChannel`` — the MessagingChannel binding (request/response; ``deliver``
    is a no-op, the tool returns ``dispatch()``'s value).
  - ``build_mcp_server(service)`` — constructs the FastMCP server, registering
    tools that each call the shared ``MessagingService``. Lazily imports ``mcp``
    so a deployment without the SDK simply doesn't enable the channel.

The ``mcp`` SDK is pinned to >=1.9,<2 in pyproject (tested against 1.27).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from devai.adapters.messaging.base import (
    ConversationReply,
    ConversationTurn,
    MessagingChannel,
)
from devai.identity import Principal

if TYPE_CHECKING:
    from devai.chat.messaging_service import MessagingService

logger = logging.getLogger(__name__)


class McpChannel(MessagingChannel):
    """Conversational turns arriving from an MCP client tool call.

    ``raw`` is ``{"message": str, "conversation_id": str | None,
    "user_id": str | None, "principal": Principal | None}``.
    """

    name = "mcp"

    async def to_turn(self, raw: Any) -> ConversationTurn | None:
        if not isinstance(raw, dict):
            return None
        text = (raw.get("message") or raw.get("text") or "").strip()
        if not text:
            return None
        conv = raw.get("conversation_id") or "default"
        principal = raw.get("principal")
        if not isinstance(principal, Principal):
            principal = Principal(email="mcp@devai", auth_provider="mcp", display_name="mcp-client")
        return ConversationTurn(
            text=text,
            conversation_id=f"mcp:{conv}",
            channel="mcp",
            user_id=raw.get("user_id"),
            principal=principal,
            metadata={"conversation_id": conv},
        )

    async def deliver(self, turn: ConversationTurn, reply: ConversationReply) -> None:
        # Request/response: the MCP tool returns dispatch()'s value.
        return None


def build_mcp_server(service: MessagingService) -> Any:
    """Build the FastMCP server exposing DevAI's tools. Returns the FastMCP obj.

    Raises ImportError if the ``mcp`` SDK is absent — the caller (app wiring)
    catches it and leaves the channel unmounted.
    """
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("devai")

    @mcp.tool()
    async def chat(message: str, conversation_id: str | None = None) -> str:
        """Hold a conversation with DevAI. Ask about pipelines, runs, repos,
        SRE incidents, security findings, or instruct it to act."""
        reply = await service.dispatch_inline("mcp", {"message": message, "conversation_id": conversation_id})
        return reply.text if reply else ""

    @mcp.tool()
    async def pipeline_status(run_id: str) -> str:
        """Get the current status of a DevAI pipeline run by its run_id."""
        reply = await service.dispatch_inline(
            "mcp",
            {
                "message": f"What is the status of pipeline run {run_id}? Summarize concisely.",
                "conversation_id": f"status-{run_id}",
            },
        )
        return reply.text if reply else ""

    @mcp.tool()
    async def trigger_pipeline(repo: str, requirements: str) -> str:
        """Ask DevAI to start work on a repo with the given requirements.

        ``repo`` is ``owner/name``; ``requirements`` is plain-language scope."""
        reply = await service.dispatch_inline(
            "mcp",
            {
                "message": (f"Trigger a pipeline for repository {repo} with these requirements: {requirements}"),
                "conversation_id": f"trigger-{repo}",
            },
        )
        return reply.text if reply else ""

    @mcp.tool()
    async def inject_requirements(run_id: str, message: str) -> str:
        """Inject an additional requirement / feedback into a RUNNING pipeline."""
        reply = await service.dispatch_inline(
            "mcp",
            {
                "message": f"For run {run_id}, inject this requirement: {message}",
                "conversation_id": f"inject-{run_id}",
            },
        )
        return reply.text if reply else ""

    return mcp


__all__ = ["McpChannel", "build_mcp_server"]
