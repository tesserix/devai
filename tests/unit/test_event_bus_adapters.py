"""Event-bus adapter tests — contract + factory + subject-matching.

Three layers:

1. **Contract tests** — every adapter we can construct in CI (Noop only,
   plus NATS when a broker is reachable) is exercised against the same
   set of behaviors: connect → publish → subscribe round-trip, wildcard
   matching, durable subscription, unsubscribe, health_check.

2. **Factory tests** — every value of DEVAI_EVENT_BUS_PROVIDER resolves
   to a usable adapter; failure modes degrade gracefully to Noop.

3. **Wildcard matching** — the Noop adapter implements NATS-style subject
   matching so developing against it transfers cleanly to JetStream.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from devai.adapters.event_bus import (
    KNOWN_PROVIDERS,
    EventBusAdapter,
    EventMessage,
    create_event_bus_adapter,
    event_bus_registry,
)
from devai.adapters.event_bus.noop import NoopEventBusAdapter, _matches

# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


class _Settings:
    """Minimal settings stub. Override per-test by subclassing."""

    event_bus_provider = "noop"
    nats_url = "nats://localhost:4222"
    nats_stream = "DEVAI_TEST"
    nats_max_deliver = 3
    nats_ack_wait = 30


async def _drain() -> None:
    """Give scheduled publish tasks a chance to run."""
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.fixture
async def noop_adapter() -> NoopEventBusAdapter:
    a = NoopEventBusAdapter()
    await a.connect()
    yield a
    await a.close()


# ──────────────────────────────────────────────────────────────────────
# Contract: any compliant adapter passes these behaviors
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_contract_connect_is_idempotent(noop_adapter):
    # Already connected via fixture — calling again is a no-op.
    await noop_adapter.connect()
    health = await noop_adapter.health_check()
    assert health["ok"] is True
    assert health["provider"] == "noop"


@pytest.mark.asyncio
async def test_contract_publish_subscribe_roundtrip(noop_adapter):
    received: list[EventMessage] = []

    async def handler(msg: EventMessage) -> None:
        received.append(msg)
        await msg.ack()

    sub = await noop_adapter.subscribe(
        "devai.pipeline.task.created",
        handler,
        durable_name="test-roundtrip",
    )

    await noop_adapter.publish(
        "devai.pipeline.task.created",
        {"task_id": "t1", "blueprint": "alm-pipeline"},
    )
    await _drain()

    assert len(received) == 1
    assert received[0].subject == "devai.pipeline.task.created"
    assert received[0].json() == {"task_id": "t1", "blueprint": "alm-pipeline"}

    await sub.unsubscribe()


@pytest.mark.asyncio
async def test_contract_publish_accepts_str_bytes_and_dict(noop_adapter):
    received: list[bytes] = []

    async def handler(msg: EventMessage) -> None:
        received.append(msg.data)

    await noop_adapter.subscribe("devai.test.payloads", handler, durable_name="t")

    await noop_adapter.publish("devai.test.payloads", "plain-string")
    await noop_adapter.publish("devai.test.payloads", b"raw-bytes")
    await noop_adapter.publish("devai.test.payloads", {"key": "value"})
    await _drain()

    assert b"plain-string" in received
    assert b"raw-bytes" in received
    assert any(b'"key"' in r and b'"value"' in r for r in received)


@pytest.mark.asyncio
async def test_contract_wildcard_one_token(noop_adapter):
    """`devai.pipeline.task.*` matches one-token suffixes."""
    received: list[str] = []

    async def handler(msg: EventMessage) -> None:
        received.append(msg.subject)

    await noop_adapter.subscribe(
        "devai.pipeline.task.*", handler, durable_name="t-onestar"
    )

    await noop_adapter.publish("devai.pipeline.task.created", b"x")
    await noop_adapter.publish("devai.pipeline.task.completed", b"x")
    # Two tokens after the prefix — should NOT match a single `*`.
    await noop_adapter.publish("devai.pipeline.task.stage.started", b"x")
    await _drain()

    assert "devai.pipeline.task.created" in received
    assert "devai.pipeline.task.completed" in received
    assert "devai.pipeline.task.stage.started" not in received


@pytest.mark.asyncio
async def test_contract_wildcard_greater_than(noop_adapter):
    """`devai.>` matches any number of trailing tokens."""
    received: list[str] = []

    async def handler(msg: EventMessage) -> None:
        received.append(msg.subject)

    await noop_adapter.subscribe("devai.>", handler, durable_name="t-greater")

    await noop_adapter.publish("devai.pipeline.task.created", b"x")
    await noop_adapter.publish("devai.pipeline.stage.review.completed", b"x")
    await noop_adapter.publish("not-devai.thing", b"x")  # mismatch
    await _drain()

    assert "devai.pipeline.task.created" in received
    assert "devai.pipeline.stage.review.completed" in received
    assert "not-devai.thing" not in received


@pytest.mark.asyncio
async def test_contract_unsubscribe_stops_delivery(noop_adapter):
    received: list[str] = []

    async def handler(msg: EventMessage) -> None:
        received.append(msg.subject)

    sub = await noop_adapter.subscribe(
        "devai.test.unsub", handler, durable_name="t-unsub"
    )
    await noop_adapter.publish("devai.test.unsub", b"first")
    await _drain()
    assert len(received) == 1

    await sub.unsubscribe()
    await noop_adapter.publish("devai.test.unsub", b"second")
    await _drain()
    assert len(received) == 1  # didn't grow


@pytest.mark.asyncio
async def test_contract_close_is_idempotent(noop_adapter):
    await noop_adapter.close()
    await noop_adapter.close()  # second call must not raise


@pytest.mark.asyncio
async def test_contract_ensure_stream_is_safe_to_call_twice(noop_adapter):
    await noop_adapter.ensure_stream("DEVAI", ["devai.>"])
    await noop_adapter.ensure_stream("DEVAI", ["devai.>"])
    health = await noop_adapter.health_check()
    assert "streams=1" in health["detail"]


# ──────────────────────────────────────────────────────────────────────
# Factory tests — graceful degradation rules
# ──────────────────────────────────────────────────────────────────────


def test_known_providers_advertise_at_least_noop_and_nats():
    assert "noop" in KNOWN_PROVIDERS
    assert "nats" in KNOWN_PROVIDERS
    # Whatever the registry advertises matches KNOWN_PROVIDERS exactly.
    assert sorted(event_bus_registry.known()) == sorted(KNOWN_PROVIDERS)


def test_factory_noop():
    settings = _Settings()
    adapter = create_event_bus_adapter(settings)
    assert adapter.provider_name == "noop"


def test_factory_unknown_provider_degrades_to_noop():
    class S(_Settings):
        event_bus_provider = "made-up"

    adapter = create_event_bus_adapter(S())
    assert adapter.provider_name == "noop"


def test_factory_nats_without_url_degrades_to_noop():
    class S(_Settings):
        event_bus_provider = "nats"
        nats_url = ""

    adapter = create_event_bus_adapter(S())
    # Either nats with the missing-url path returned Noop, or nats-py not
    # installed — either way we expect noop here.
    assert adapter.provider_name == "noop"


def test_factory_returns_adapter_instance():
    adapter = create_event_bus_adapter(_Settings())
    assert isinstance(adapter, EventBusAdapter)


# ──────────────────────────────────────────────────────────────────────
# Subject-matching unit tests (no adapter needed)
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "pattern,subject,expected",
    [
        ("devai.pipeline.task.created", "devai.pipeline.task.created", True),
        ("devai.>", "devai.pipeline.task.created", True),
        ("devai.*.task.created", "devai.pipeline.task.created", True),
        ("devai.pipeline.*", "devai.pipeline.task", True),
        ("devai.pipeline.*", "devai.pipeline.task.created", False),
        ("devai.pipeline.task.*", "devai.pipeline.task.created", True),
        ("devai.>", "other.thing", False),
        ("devai.pipeline.>", "devai.pipeline", False),
        ("devai.pipeline.>", "devai.pipeline.task", True),
        ("a.b.c", "a.b.c", True),
        ("a.b.c", "a.b.c.d", False),
    ],
)
def test_subject_matches(pattern, subject, expected):
    assert _matches(pattern, subject) is expected


# ──────────────────────────────────────────────────────────────────────
# Real-NATS contract tests — only when a broker is reachable
# ──────────────────────────────────────────────────────────────────────

_NATS_URL = os.environ.get("DEVAI_TEST_NATS_URL", "")


@pytest.mark.asyncio
@pytest.mark.skipif(
    not _NATS_URL, reason="DEVAI_TEST_NATS_URL not set — skipping live NATS tests"
)
async def test_nats_adapter_publish_subscribe_roundtrip():
    """Exercise the JetStream adapter against a real broker.

    Requires `DEVAI_TEST_NATS_URL` to be set (e.g.
    `nats://localhost:4222` after `docker run -p 4222:4222 nats:latest -js`).
    """
    from devai.adapters.event_bus.nats_adapter import NatsEventBusAdapter

    adapter = NatsEventBusAdapter(
        url=_NATS_URL,
        default_stream="DEVAI_PYTEST",
        default_subjects=("devai.pytest.>",),
    )
    await adapter.connect()
    try:
        received: list[EventMessage] = []

        async def handler(msg: EventMessage) -> None:
            received.append(msg)
            await msg.ack()

        sub = await adapter.subscribe(
            "devai.pytest.task.>",
            handler,
            durable_name="pytest-roundtrip",
        )

        await adapter.publish(
            "devai.pytest.task.created", {"task_id": "live-1"}
        )
        # Allow JetStream to deliver
        for _ in range(20):
            if received:
                break
            await asyncio.sleep(0.05)

        assert received, "no message received from NATS within timeout"
        assert received[0].json() == {"task_id": "live-1"}

        await sub.unsubscribe()
    finally:
        await adapter.close()
