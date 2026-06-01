"""Regression tests for the NATS consumer ack/nak semantics (P0 fix).

Bug: ``_handle_message`` acked in a ``finally`` block even when the handler
raised, so a failed message was acknowledged and never redelivered — silently
dropping work and defeating JetStream ``max_deliver``.

Fix: ack only on success; nak (retryable) on failure, term (poison) past the
delivery ceiling.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from devai.core.base_agent import _delivery_count


class _Msg:
    """A fake JetStream message recording ack/nak/term calls."""

    def __init__(self, data: bytes = b"{}", num_delivered: int = 1) -> None:
        self.data = data
        self.metadata = SimpleNamespace(num_delivered=num_delivered)
        self.acked = False
        self.naked = False
        self.termed = False

    async def ack(self) -> None:
        self.acked = True

    async def nak(self) -> None:
        self.naked = True

    async def term(self) -> None:
        self.termed = True


def _make_agent(max_deliver: int = 3):
    """Build a minimal concrete BaseAgent exercising only _nak_or_term + the
    ack decision, without standing up the full agent/graph machinery."""
    from devai.core.base_agent import BaseAgent

    class _Agent(BaseAgent):
        async def _execute_graph(self, state, a2a):  # noqa: ANN001
            return state

    agent = _Agent.__new__(_Agent)  # bypass __init__
    agent.name = "test-agent"
    agent.config = SimpleNamespace(nats_max_deliver=max_deliver)
    return agent


def test_delivery_count_reads_metadata():
    assert _delivery_count(_Msg(num_delivered=2)) == 2
    assert _delivery_count(object()) == 0  # no metadata → 0


def test_nak_on_first_failure():
    agent = _make_agent(max_deliver=3)
    msg = _Msg(num_delivered=1)
    asyncio.run(agent._nak_or_term(msg))
    assert msg.naked is True
    assert msg.termed is False
    assert msg.acked is False


def test_term_at_max_deliver():
    agent = _make_agent(max_deliver=3)
    msg = _Msg(num_delivered=3)
    asyncio.run(agent._nak_or_term(msg))
    assert msg.termed is True
    assert msg.naked is False


def test_failure_path_never_acks():
    """The whole point: a failed message must NOT be acked."""
    agent = _make_agent(max_deliver=3)
    msg = _Msg(num_delivered=1)
    asyncio.run(agent._nak_or_term(msg))
    assert msg.acked is False


def test_nak_or_term_tolerates_double_without_nak():
    """A test/double message with only ack() must not raise."""
    agent = _make_agent()
    msg = MagicMock(spec=["ack"])  # no nak/term/metadata
    msg.ack = AsyncMock()
    asyncio.run(agent._nak_or_term(msg))
    msg.ack.assert_not_called()  # we do NOT ack on the failure path
