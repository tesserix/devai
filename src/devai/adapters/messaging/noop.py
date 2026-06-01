"""Noop messaging channel — the mandatory graceful-degrade fallback.

Used in tests, in disabled mode, and whenever a transport's SDK/config is
missing. It parses nothing and delivers nothing, so wiring it in is always
safe.
"""

from __future__ import annotations

from typing import Any

from devai.adapters.messaging.base import (
    ConversationReply,
    ConversationTurn,
    MessagingChannel,
)


class NoopMessagingChannel(MessagingChannel):
    """A channel that ignores everything. Always safe to mount."""

    name = "noop"

    async def to_turn(self, raw: Any) -> ConversationTurn | None:
        return None

    async def deliver(self, turn: ConversationTurn, reply: ConversationReply) -> None:
        return None

    async def health_check(self) -> dict[str, Any]:
        return {"ok": True, "provider": "noop", "detail": "messaging disabled"}


__all__ = ["NoopMessagingChannel"]
