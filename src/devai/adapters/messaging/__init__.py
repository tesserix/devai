"""Messaging adapter family — inbound conversational transports.

Public surface re-exported here so callers do:

    from devai.adapters.messaging import (
        MessagingChannel, ConversationTurn, ConversationReply,
        create_messaging_channels,
    )

Concrete channels (slack, remote_url, mcp) lazily import their SDKs, so this
package import is cheap and never pulls in slack_sdk / mcp unless a channel is
actually constructed.
"""

from __future__ import annotations

from devai.adapters.messaging.base import (
    ConversationGatewayProtocol,
    ConversationReply,
    ConversationTurn,
    DeliveryFn,
    MessagingChannel,
)
from devai.adapters.messaging.factory import create_messaging_channels
from devai.adapters.messaging.noop import NoopMessagingChannel

__all__ = [
    "ConversationGatewayProtocol",
    "ConversationReply",
    "ConversationTurn",
    "DeliveryFn",
    "MessagingChannel",
    "NoopMessagingChannel",
    "create_messaging_channels",
]
