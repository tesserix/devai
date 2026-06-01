"""Messaging adapter family — transport-agnostic conversational channels.

This family lets a human (or another agent) hold a conversation with DevAI from
*any* transport — Slack, a remote URL/thread, an MCP client — without ever
duplicating the conversational brain.

The shape:

  - ``ConversationTurn``  — one inbound message, normalized across transports.
  - ``ConversationReply`` — one outbound answer.
  - ``ConversationGatewayProtocol`` — the single brain. Every channel calls
    ``handle_turn`` and nothing else. The concrete gateway lives in
    ``devai.chat.gateway`` (it wraps the existing ChatAgent + Redis sessions +
    A2A); this module only declares the protocol so the adapter layer doesn't
    import the chat agent directly.
  - ``MessagingChannel`` — the family ABC. A transport binding: parse the raw
    transport payload into a ``ConversationTurn``, hand it to the gateway, emit
    the reply over its own wire. ``dispatch()`` is the **never-raise** boundary
    every transport inherits — any failure logs and degrades to noop, exactly
    like every other adapter family (CLAUDE.md §6).

``conversation_id`` is the universal thread key (``slack:{channel}:{ts}``,
``url:{thread}``, ``mcp:{session}``) so the same conversation keeps one history
in Redis regardless of which transport it arrived on.
"""

from __future__ import annotations

import logging
from abc import abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from devai.adapters.base import Adapter

if TYPE_CHECKING:
    from devai.identity import Principal

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ConversationTurn:
    """A single inbound conversational message — transport-neutral.

    ``conversation_id`` is the stable thread key used for session continuity.
    ``principal`` attributes the turn to a real human/agent for audit + A2A.
    """

    text: str
    conversation_id: str
    channel: str = "unknown"  # "slack" | "remote_url" | "mcp"
    user_id: str | None = None
    principal: Principal | None = None
    trace_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def redacted(self) -> dict[str, Any]:
        """A log-safe view (message text elided) for structured logging."""
        return {
            "conversation_id": self.conversation_id,
            "channel": self.channel,
            "user_id": self.user_id,
            "trace_id": self.trace_id,
            "text_len": len(self.text or ""),
        }


@dataclass(slots=True)
class ConversationReply:
    """A single outbound answer — transport-neutral."""

    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class ConversationGatewayProtocol(Protocol):
    """The one place conversational logic lives.

    Implemented by ``devai.chat.gateway.ConversationGateway``. Channels depend
    only on this protocol, never on the concrete chat agent.
    """

    async def handle_turn(self, turn: ConversationTurn) -> ConversationReply:
        """Run one conversational turn and return the reply. Must not raise."""
        ...


# A delivery hook a push-transport can register so a background worker (NATS)
# can post the reply back after the original request has already been acked.
DeliveryFn = Callable[[ConversationTurn, ConversationReply], Awaitable[None]]


class MessagingChannel(Adapter):
    """Base class for every inbound conversational transport.

    Subclasses implement two tiny methods:
      - ``to_turn(raw)``  — parse the transport payload into a ConversationTurn
        (or return ``None`` to ignore, e.g. a bot's own echo).
      - ``deliver(turn, reply)`` — emit the reply over this transport. For
        request/response transports (remote URL, MCP) this is a no-op and the
        caller reads ``dispatch()``'s return value; for push transports (Slack)
        it posts back via the transport's API.

    Everything else — the never-raise boundary, logging — is inherited.
    """

    #: Set by subclass. Used in logs + health output.
    name: str = "abstract"

    def __init__(self, gateway: ConversationGatewayProtocol) -> None:
        self.gateway = gateway

    @abstractmethod
    async def to_turn(self, raw: Any) -> ConversationTurn | None:
        """Parse a raw transport payload into a ConversationTurn, or None."""

    @abstractmethod
    async def deliver(self, turn: ConversationTurn, reply: ConversationReply) -> None:
        """Emit a reply over this transport (push transports only; else no-op)."""

    async def dispatch(self, raw: Any) -> ConversationReply | None:
        """Template method — the never-raise boundary every channel shares.

        Parse → gateway → deliver. Any exception is logged and swallowed
        (degrade to noop) so a failed turn never crashes a Slack ack, an MCP
        tool call, or an HTTP request.
        """
        try:
            turn = await self.to_turn(raw)
            if turn is None:
                return None
            reply = await self.gateway.handle_turn(turn)
            await self.deliver(turn, reply)
            return reply
        except Exception:
            logger.exception("[%s] dispatch failed — degrading to noop", self.name)
            return None

    # --- Adapter contract -------------------------------------------------

    async def close(self) -> None:
        return None

    async def health_check(self) -> dict[str, Any]:
        return {"ok": True, "provider": self.name, "detail": "messaging channel"}


__all__ = [
    "ConversationGatewayProtocol",
    "ConversationReply",
    "ConversationTurn",
    "DeliveryFn",
    "MessagingChannel",
]
