"""MessagingService — wires the conversational channels to the gateway + NATS.

Two responsibilities:

1. **Inline dispatch** — for request/response transports (remote URL, MCP) the
   caller just wants the answer back synchronously. ``dispatch_inline`` runs the
   turn through the gateway and returns the reply.

2. **Worker handoff** — for push transports with a tight ack budget (Slack must
   200 within 3 s) the turn is published to NATS (``messaging_turn_subject``)
   and a durable, queue-grouped subscriber processes it later and delivers the
   reply back over the originating transport. ``enqueue_turn`` publishes;
   ``start`` subscribes the worker. If NATS is unavailable, ``enqueue_turn``
   falls back to an in-process background task so the feature still works in a
   minimal/local deployment.

The service owns the ``{name: MessagingChannel}`` map (built by the factory) so
the worker can route a processed turn back to the right transport's
``deliver``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

from devai.adapters.messaging.base import (
    ConversationReply,
    ConversationTurn,
    MessagingChannel,
)
from devai.chat.gateway import ConversationGateway
from devai.identity import Principal

if TYPE_CHECKING:
    from devai.adapters.event_bus.base import EventBusAdapter, EventMessage
    from devai.config import Settings
    from devai.core.state import StateManager
    from devai.services.database import Database

logger = logging.getLogger(__name__)


class MessagingService:
    """Owns the gateway, the channel map, and the NATS turn worker."""

    def __init__(
        self,
        config: Settings,
        state_manager: StateManager,
        *,
        database: Database | None = None,
        event_bus_adapter: EventBusAdapter | None = None,
        settings_service: Any = None,
    ) -> None:
        self.config = config
        self.state_manager = state_manager
        self.database = database
        self.event_bus_adapter = event_bus_adapter
        self.gateway = ConversationGateway(
            config, state_manager, database=database, settings_service=settings_service
        )
        self.channels: dict[str, MessagingChannel] = {}
        self._subscription: Any = None
        self._bg_tasks: set[asyncio.Task[Any]] = set()
        self._subject = getattr(config, "messaging_turn_subject", "devai.chat.turn")

    # --- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        """Build channels and, if a worker is configured, subscribe to NATS."""
        from devai.adapters.messaging.factory import create_messaging_channels

        self.channels = create_messaging_channels(self.config, self.gateway)

        if not getattr(self.config, "messaging_use_worker", True):
            return
        if self.event_bus_adapter is None:
            logger.info("messaging worker: no event bus — turns run in-process")
            return
        try:
            await self.event_bus_adapter.ensure_stream(
                "DEVAI_CHAT", [f"{self._subject}.>", self._subject]
            )
            self._subscription = await self.event_bus_adapter.subscribe(
                self._subject,
                self._on_turn_message,
                durable_name="devai-chat-turn-worker",
                queue_group="devai-chat-turn",
            )
            logger.info("messaging worker subscribed to %s", self._subject)
        except Exception:
            logger.exception("messaging worker subscribe failed — falling back to in-process")
            self._subscription = None

    async def stop(self) -> None:
        if self._subscription is not None:
            try:
                await self._subscription.unsubscribe()
            except Exception:
                logger.debug("messaging worker unsubscribe failed", exc_info=True)
            self._subscription = None
        for task in list(self._bg_tasks):
            task.cancel()
        self._bg_tasks.clear()

    # --- inline path (remote URL, MCP) -----------------------------------

    async def dispatch_inline(
        self, channel_name: str, raw: Any
    ) -> ConversationReply | None:
        """Run a turn synchronously and return the reply (request/response)."""
        channel = self.channels.get(channel_name)
        if channel is None:
            logger.warning("dispatch_inline: channel %s not enabled", channel_name)
            return None
        return await channel.dispatch(raw)

    # --- worker path (Slack) ---------------------------------------------

    async def enqueue_turn(self, turn: ConversationTurn) -> None:
        """Hand a turn to the NATS worker (ack-fast), or run it in-process.

        Used by push transports that must ack their HTTP request immediately.
        The worker reconstructs the turn, runs the gateway, and delivers the
        reply back over the originating channel.
        """
        envelope = _turn_to_envelope(turn)
        if self.event_bus_adapter is not None and self._subscription is not None:
            try:
                await self.event_bus_adapter.publish(self._subject, envelope)
                return
            except Exception:
                logger.exception("enqueue_turn publish failed — running in-process")
        # Fallback: in-process background task (still never blocks the caller).
        task = asyncio.create_task(self._process_turn(turn))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _on_turn_message(self, msg: EventMessage) -> None:
        """NATS worker callback: decode, process, deliver, ack."""
        try:
            turn = _envelope_to_turn(msg.json())
            await self._process_turn(turn)
            await msg.ack()
        except Exception:
            logger.exception("messaging worker failed to process turn")
            # Don't redeliver a poison turn forever — terminate it.
            try:
                await msg.term()
            except Exception:
                logger.debug("term failed", exc_info=True)

    async def _process_turn(self, turn: ConversationTurn) -> None:
        """Run the gateway and deliver the reply back over the turn's channel."""
        reply = await self.gateway.handle_turn(turn)
        channel = self.channels.get(turn.channel)
        if channel is None:
            logger.warning("processed turn for unknown channel %s — dropping reply", turn.channel)
            return
        try:
            await channel.deliver(turn, reply)
        except Exception:
            logger.exception("deliver failed for channel %s", turn.channel)


# --- (de)serialization for the NATS envelope -----------------------------


def _turn_to_envelope(turn: ConversationTurn) -> dict[str, Any]:
    return {
        "text": turn.text,
        "conversation_id": turn.conversation_id,
        "channel": turn.channel,
        "user_id": turn.user_id,
        "principal": turn.principal.to_dict() if turn.principal else None,
        "trace_id": turn.trace_id,
        "metadata": turn.metadata,
    }


def _envelope_to_turn(data: dict[str, Any]) -> ConversationTurn:
    return ConversationTurn(
        text=data.get("text", ""),
        conversation_id=data.get("conversation_id", "default"),
        channel=data.get("channel", "unknown"),
        user_id=data.get("user_id"),
        principal=Principal.from_dict(data.get("principal")),
        trace_id=data.get("trace_id", ""),
        metadata=data.get("metadata") or {},
    )


def _json(data: dict[str, Any]) -> str:
    return json.dumps(data)


__all__ = ["MessagingService"]
