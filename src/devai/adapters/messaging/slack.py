"""Slack channel — talk to DevAI by @mentioning it (or DMing it) in Slack.

Uses Slack's HTTP Events API (stateless, scales behind the existing Istio
ingress; Socket Mode is a local-dev alternative we don't wire here). The flow:

  1. Slack POSTs an event to ``/webhook/slack``.
  2. We verify the request signature (v0 HMAC-SHA256 over ``v0:{ts}:{raw_body}``,
     5-minute replay window, constant-time) — mirrors the webhook hardening.
  3. ``url_verification`` challenges are echoed back immediately.
  4. For a real event we ack 200 within Slack's 3s budget and hand the turn to
     the MessagingService (NATS worker). The worker runs the gateway and posts
     the reply back **in-thread** via ``chat.postMessage``.
  5. Slack retries (``X-Slack-Retry-Num``) are de-duped on ``event_id`` in Redis.

``deliver`` is the push side: it calls Slack's Web API to post the reply.

``slack_sdk`` is imported lazily so a deployment without it (Slack disabled)
never needs the dep.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from typing import TYPE_CHECKING, Any

from devai.adapters.messaging.base import (
    ConversationReply,
    ConversationTurn,
    MessagingChannel,
)
from devai.identity import Principal

if TYPE_CHECKING:
    from devai.adapters.messaging.base import ConversationGatewayProtocol

logger = logging.getLogger(__name__)

_REPLAY_WINDOW_SECONDS = 60 * 5


def verify_slack_signature(
    signing_secret: str,
    timestamp: str,
    signature: str,
    raw_body: bytes,
) -> bool:
    """Verify a Slack request signature (v0 = HMAC-SHA256).

    Hash the RAW body bytes (before any JSON parse), enforce a 5-minute replay
    window, and compare in constant time.
    """
    if not signing_secret or not timestamp or not signature:
        return False
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs(time.time() - ts) > _REPLAY_WINDOW_SECONDS:
        return False
    basestring = b"v0:" + timestamp.encode() + b":" + raw_body
    expected = "v0=" + hmac.new(signing_secret.encode(), basestring, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


class SlackChannel(MessagingChannel):
    """Slack Events API transport over the shared gateway."""

    name = "slack"

    def __init__(self, gateway: ConversationGatewayProtocol, settings: Any) -> None:
        super().__init__(gateway)
        self.settings = settings
        self.bot_token = getattr(settings, "slack_bot_token", "") or ""
        self.signing_secret = getattr(settings, "slack_signing_secret", "") or ""
        allowed = getattr(settings, "slack_allowed_channels", "") or ""
        self.allowed_channels = {c.strip() for c in allowed.split(",") if c.strip()}
        self._client: Any = None

    # --- inbound parsing --------------------------------------------------

    async def to_turn(self, raw: Any) -> ConversationTurn | None:
        """Parse a Slack Events API payload into a ConversationTurn, or None.

        Ignores bot/self echoes, non-message events, and disallowed channels.
        """
        if not isinstance(raw, dict):
            return None
        event = raw.get("event") or {}
        etype = event.get("type")
        if etype not in ("app_mention", "message"):
            return None
        # Ignore the bot's own messages / other bots / edits.
        if event.get("bot_id") or event.get("subtype"):
            return None

        channel_id = event.get("channel", "")
        if self.allowed_channels and channel_id not in self.allowed_channels:
            logger.info("slack: ignoring message from non-allowlisted channel %s", channel_id)
            return None

        text = _strip_mention(event.get("text", "")).strip()
        if not text:
            return None

        thread_ts = event.get("thread_ts") or event.get("ts")
        user = event.get("user", "")
        team = raw.get("team_id", "")
        principal = Principal(
            email=f"{user}@slack" if user else "slack@devai",
            uid=user,
            tenant_id=team,
            auth_provider="slack",
            display_name=user or "slack-user",
        )
        return ConversationTurn(
            text=text,
            conversation_id=f"slack:{channel_id}:{thread_ts}",
            channel="slack",
            user_id=user,
            principal=principal,
            metadata={"channel_id": channel_id, "thread_ts": thread_ts},
        )

    # --- outbound delivery (push) ----------------------------------------

    async def deliver(self, turn: ConversationTurn, reply: ConversationReply) -> None:
        """Post the reply back into the originating Slack thread."""
        client = self._get_client()
        if client is None:
            logger.warning("slack: no bot token — cannot deliver reply")
            return
        await client.chat_postMessage(
            channel=turn.metadata["channel_id"],
            thread_ts=turn.metadata["thread_ts"],
            text=reply.text or "_(no response)_",
        )

    def _get_client(self) -> Any:
        if self._client is None and self.bot_token:
            try:
                from slack_sdk.web.async_client import AsyncWebClient

                self._client = AsyncWebClient(token=self.bot_token)
            except Exception:
                logger.exception("slack: slack_sdk not available")
                self._client = None
        return self._client

    async def close(self) -> None:
        self._client = None


def _strip_mention(text: str) -> str:
    """Remove a leading ``<@BOTID>`` mention token from app_mention text."""
    if text.startswith("<@"):
        end = text.find(">")
        if end != -1:
            return text[end + 1 :]
    return text


__all__ = ["SlackChannel", "verify_slack_signature"]
