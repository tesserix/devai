"""Tests for the transport-agnostic messaging core.

Covers the ConversationGateway brain + the MessagingChannel never-raise
boundary + the Noop and RemoteUrl channels. No external SDKs touched.
"""

from __future__ import annotations

import pytest

from devai.adapters.messaging import (
    ConversationReply,
    ConversationTurn,
    NoopMessagingChannel,
    create_messaging_channels,
)
from devai.adapters.messaging.remote_url import RemoteUrlChannel
from devai.identity import Principal


class _FakeGateway:
    """Records turns and returns a canned reply."""

    def __init__(self, reply: str = "ok", boom: bool = False) -> None:
        self.reply = reply
        self.boom = boom
        self.turns: list[ConversationTurn] = []

    async def handle_turn(self, turn: ConversationTurn) -> ConversationReply:
        self.turns.append(turn)
        if self.boom:
            raise RuntimeError("gateway exploded")
        return ConversationReply(text=f"{self.reply}:{turn.text}")


# ---- Noop ----


@pytest.mark.asyncio
async def test_noop_channel_ignores_everything() -> None:
    ch = NoopMessagingChannel(_FakeGateway())
    assert await ch.dispatch({"text": "hi"}) is None
    health = await ch.health_check()
    assert health["ok"] is True and health["provider"] == "noop"


# ---- RemoteUrl parsing + dispatch ----


@pytest.mark.asyncio
async def test_remote_url_parses_and_dispatches() -> None:
    gw = _FakeGateway(reply="echo")
    ch = RemoteUrlChannel(gw)
    reply = await ch.dispatch({"text": "hello", "thread_id": "t1"})
    assert reply is not None
    assert reply.text == "echo:hello"
    assert gw.turns[0].conversation_id == "url:t1"
    assert gw.turns[0].channel == "remote_url"


@pytest.mark.asyncio
async def test_remote_url_ignores_blank_and_nondict() -> None:
    ch = RemoteUrlChannel(_FakeGateway())
    assert await ch.dispatch({"text": "  "}) is None
    assert await ch.dispatch("not a dict") is None
    assert await ch.dispatch({"thread_id": "t"}) is None


@pytest.mark.asyncio
async def test_remote_url_threads_principal() -> None:
    gw = _FakeGateway()
    ch = RemoteUrlChannel(gw)
    p = Principal(email="dev@x", auth_provider="token")
    await ch.dispatch({"text": "hi", "thread_id": "t", "principal": p})
    assert gw.turns[0].principal is p


# ---- never-raise boundary ----


@pytest.mark.asyncio
async def test_dispatch_never_raises_on_gateway_error() -> None:
    ch = RemoteUrlChannel(_FakeGateway(boom=True))
    # Gateway raises, but dispatch must swallow and return None.
    assert await ch.dispatch({"text": "hi", "thread_id": "t"}) is None


# ---- factory ----


class _Settings:
    remote_chat_enabled = True
    mcp_server_enabled = False
    slack_enabled = False
    slack_bot_token = ""
    slack_signing_secret = ""


@pytest.mark.asyncio
async def test_factory_builds_only_enabled_channels() -> None:
    channels = create_messaging_channels(_Settings(), _FakeGateway())
    assert set(channels) == {"remote_url"}
    assert isinstance(channels["remote_url"], RemoteUrlChannel)


def test_factory_skips_slack_without_creds() -> None:
    class S(_Settings):
        slack_enabled = True  # but no token/secret

    channels = create_messaging_channels(S(), _FakeGateway())
    assert "slack" not in channels
