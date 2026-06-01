"""Factory for the messaging channel family.

``create_messaging_channels`` reads the per-channel ``DEVAI_*_ENABLED`` flags
and returns the set of constructed channels. It NEVER raises: a channel whose
SDK is missing or whose config is incomplete is skipped with a warning (the
overall result simply omits it), so a misconfigured pod degrades instead of
crashing — the same contract every adapter family follows.

Channels are constructed lazily and import their SDKs inside the constructor,
so importing this module costs nothing until a channel is actually enabled.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from devai.adapters.messaging.base import ConversationGatewayProtocol, MessagingChannel

logger = logging.getLogger(__name__)


def create_messaging_channels(
    settings: Any,
    gateway: ConversationGatewayProtocol,
) -> dict[str, MessagingChannel]:
    """Build every enabled inbound conversational channel.

    Returns a ``{name: channel}`` map. Disabled or unconstructable channels are
    omitted. The Remote-URL and MCP channels are pure (no SDK); Slack lazily
    imports ``slack_sdk``.
    """
    channels: dict[str, MessagingChannel] = {}

    if getattr(settings, "remote_chat_enabled", False):
        try:
            from devai.adapters.messaging.remote_url import RemoteUrlChannel

            channels["remote_url"] = RemoteUrlChannel(gateway)
        except Exception:
            logger.exception("remote_url channel construction failed — skipping")

    if getattr(settings, "mcp_server_enabled", False):
        try:
            from devai.adapters.messaging.mcp import McpChannel

            channels["mcp"] = McpChannel(gateway)
        except Exception:
            logger.exception("mcp channel construction failed — skipping")

    if getattr(settings, "slack_enabled", False):
        if not getattr(settings, "slack_signing_secret", "") or not getattr(settings, "slack_bot_token", ""):
            logger.warning("slack_enabled but bot token / signing secret missing — skipping Slack channel")
        else:
            try:
                from devai.adapters.messaging.slack import SlackChannel

                channels["slack"] = SlackChannel(gateway, settings)
            except Exception:
                logger.exception("slack channel construction failed — skipping")

    logger.info("messaging channels enabled: %s", sorted(channels.keys()) or ["none"])
    return channels


__all__ = ["create_messaging_channels"]
