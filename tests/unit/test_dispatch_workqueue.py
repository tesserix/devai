"""Contract tests for the NATS WorkQueue dispatch backend + the EventMessage
ack-deadline plumbing it relies on. No live NATS — a recording fake adapter."""

from __future__ import annotations

import asyncio
from typing import Any

from devai.adapters.event_bus.base import EventBusAdapter, EventMessage, MessageHandler, Subscription
from devai.pipeline.dispatch import NatsWorkQueueBackend


class FakeBus(EventBusAdapter):
    provider_name = "fake"

    def __init__(self) -> None:
        self.published: list[tuple[str, Any, dict[str, str]]] = []
        self.subscribed: list[dict[str, Any]] = []
        self.streams: list[tuple[str, list[str], dict[str, Any]]] = []

    async def connect(self) -> None:  # pragma: no cover - trivial
        return None

    async def ensure_stream(self, name: str, subjects: list[str], **kw: Any) -> None:
        self.streams.append((name, subjects, kw))

    async def publish(self, subject: str, data: Any, *, headers: dict[str, str] | None = None) -> None:
        self.published.append((subject, data, headers or {}))

    async def subscribe(
        self,
        subject: str,
        handler: MessageHandler,
        *,
        durable_name: str,
        queue_group: str | None = None,
        manual_ack: bool = True,
        max_deliver: int | None = None,
        ack_wait_seconds: int | None = None,
    ) -> Subscription:
        self.subscribed.append(
            {
                "subject": subject,
                "durable_name": durable_name,
                "queue_group": queue_group,
                "manual_ack": manual_ack,
                "max_deliver": max_deliver,
                "ack_wait_seconds": ack_wait_seconds,
            }
        )
        return Subscription(subject=subject, durable_name=durable_name, queue_group=queue_group, _adapter=self)

    async def _unsubscribe(self, sub: Subscription) -> None:  # pragma: no cover
        return None


def _backend(bus: FakeBus, *, lane: str = "alm", ack_wait: int = 300, max_deliver: int = 3) -> NatsWorkQueueBackend:
    return NatsWorkQueueBackend(
        bus,
        stream="DEVAI_PIPELINE_RUNS",
        subject_prefix="devai.pipeline.run",
        lane=lane,
        queue_group="devai-pipeline-workers",
        ack_wait_seconds=ack_wait,
        max_deliver=max_deliver,
    )


# ── subjects ──────────────────────────────────────────────────────────────


def test_lane_subjects() -> None:
    b = _backend(FakeBus(), lane="sre")
    assert b.subject == "devai.pipeline.run.sre"
    assert b.dlq_subject == "devai.pipeline.run.dlq"


# ── produce ───────────────────────────────────────────────────────────────


async def test_enqueue_publishes_with_dedup_id() -> None:
    bus = FakeBus()
    await _backend(bus).enqueue("run-1")
    assert bus.published == [("devai.pipeline.run.alm", {"task_id": "run-1"}, {"Nats-Msg-Id": "run-1"})]


async def test_dead_letter_goes_to_dlq_subject() -> None:
    bus = FakeBus()
    await _backend(bus).dead_letter("run-9", "boom", attempts=3)
    subj, data, _ = bus.published[0]
    assert subj == "devai.pipeline.run.dlq"
    assert data["task_id"] == "run-9" and data["reason"] == "boom" and data["attempts"] == 3


# ── stream + consumer config ──────────────────────────────────────────────


async def test_ensure_declares_workqueue_stream() -> None:
    bus = FakeBus()
    await _backend(bus).ensure()
    name, subjects, kw = bus.streams[0]
    assert name == "DEVAI_PIPELINE_RUNS"
    assert subjects == ["devai.pipeline.run.*"]
    assert kw.get("work_queue") is True


async def test_subscribe_binds_queue_group_and_limits() -> None:
    bus = FakeBus()
    b = _backend(bus, lane="sre", ack_wait=120, max_deliver=5)
    await b.subscribe(lambda _m: asyncio.sleep(0))  # type: ignore[arg-type,return-value]
    s = bus.subscribed[0]
    assert s["subject"] == "devai.pipeline.run.sre"
    assert s["durable_name"] == "pipeline-sre"
    assert s["queue_group"] == "devai-pipeline-workers"
    assert s["manual_ack"] is True
    assert s["max_deliver"] == 5
    assert s["ack_wait_seconds"] == 120


# ── attempt accounting / dead-letter decision ─────────────────────────────


def test_is_final_attempt() -> None:
    b = _backend(FakeBus(), max_deliver=3)
    assert b.is_final_attempt(EventMessage(subject="s", data=b"", num_delivered=1)) is False
    assert b.is_final_attempt(EventMessage(subject="s", data=b"", num_delivered=2)) is False
    assert b.is_final_attempt(EventMessage(subject="s", data=b"", num_delivered=3)) is True
    assert b.is_final_attempt(EventMessage(subject="s", data=b"", num_delivered=4)) is True


# ── keepalive heartbeat ───────────────────────────────────────────────────


async def test_keepalive_enters_exits_and_cancels_cleanly() -> None:
    calls = {"n": 0}

    async def _in_progress() -> None:
        calls["n"] += 1

    msg = EventMessage(subject="s", data=b"")
    msg._in_progress = _in_progress
    b = _backend(FakeBus())
    async with b.keepalive(msg):
        await asyncio.sleep(0)  # no beat within the floor interval — just clean enter/exit
    # the background beat task must be cancelled on exit (no leak); fast path → 0 beats
    assert calls["n"] == 0


# ── EventMessage ack-deadline plumbing ────────────────────────────────────


async def test_event_message_in_progress_and_acks() -> None:
    hit = {"ack": 0, "nack": 0, "term": 0, "ip": 0}

    async def mk(key: str):
        async def _cb() -> None:
            hit[key] += 1

        return _cb

    msg = EventMessage(subject="s", data=b"{}", num_delivered=2)
    msg._ack = await mk("ack")
    msg._nack = await mk("nack")
    msg._term = await mk("term")
    msg._in_progress = await mk("ip")
    await msg.ack()
    await msg.nack()
    await msg.term()
    await msg.in_progress()
    assert hit == {"ack": 1, "nack": 1, "term": 1, "ip": 1}
    assert msg.num_delivered == 2


async def test_event_message_noop_callbacks_are_safe() -> None:
    # An envelope with no bound callbacks (noop backend) must not raise.
    msg = EventMessage(subject="s", data=b"")
    await msg.ack()
    await msg.nack()
    await msg.term()
    await msg.in_progress()
    assert msg.num_delivered == 1
