"""Remote-URL channel — a plain HTTP/SSE thread transport.

This is the "its own remote URL thread" channel: any remote app can POST a
message to a thread and read the reply, with no SDK and no Slack/MCP machinery.
It's a request/response transport, so ``deliver`` is a no-op — the route reads
``dispatch()``'s return value (or streams it).

Routes that use this channel live in ``devai.chat.remote_routes``.
"""

from __future__ import annotations

from typing import Any

from devai.adapters.messaging.base import (
    ConversationReply,
    ConversationTurn,
    MessagingChannel,
)
from devai.identity import Principal


class RemoteUrlChannel(MessagingChannel):
    """Conversational turns arriving over a plain remote HTTP/SSE thread.

    ``raw`` is a dict the route assembles:
        {
          "text": str,
          "thread_id": str,
          "principal": Principal | None,
          "trace_id": str,
          "user_id": str | None,
        }
    """

    name = "remote_url"

    async def to_turn(self, raw: Any) -> ConversationTurn | None:
        if not isinstance(raw, dict):
            return None
        text = (raw.get("text") or "").strip()
        if not text:
            return None
        thread_id = raw.get("thread_id") or "default"
        principal = raw.get("principal")
        if not isinstance(principal, Principal):
            principal = None
        return ConversationTurn(
            text=text,
            conversation_id=f"url:{thread_id}",
            channel="remote_url",
            user_id=raw.get("user_id"),
            principal=principal,
            trace_id=raw.get("trace_id") or "",
            metadata={"thread_id": thread_id},
        )

    async def deliver(self, turn: ConversationTurn, reply: ConversationReply) -> None:
        # Request/response transport: the route returns dispatch()'s value.
        return None


__all__ = ["RemoteUrlChannel"]
