"""NATS JetStream WorkQueue dispatch backend for pipeline runs.

The pipeline can dispatch runs three ways (``DEVAI_PIPELINE_DISPATCH_BACKEND``):

  * ``redis``  — the hand-rolled durable claim queue in core/state.py (default).
  * ``nats``   — this backend: a JetStream WorkQueue with native single-delivery,
                 ack_wait redelivery, max_deliver dead-lettering, and lane routing.
  * ``inproc`` — the per-pod in-memory queue (tests / CLI).

This module owns ONLY the queue mechanics (enqueue / subscribe / dead-letter /
attempt accounting) so they're testable in isolation; ``pipeline.py`` wires the
message handler to its execution body (load snapshot → stop-guard → execute →
ack/nack/term). The run snapshot stays in Redis/Postgres (system of record); the
queue carries just the ``task_id``.

Lane routing kills the api↔sre release ping-pong: each service publishes to AND
subscribes on its own lane subject (``devai.pipeline.run.<lane>``), so a run never
lands on a service that can't run its blueprint.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from devai.adapters.event_bus.base import EventBusAdapter, EventMessage, MessageHandler, Subscription

logger = logging.getLogger(__name__)


class NatsWorkQueueBackend:
    """Thin WorkQueue surface over the event-bus adapter.

    Construct with a connected adapter (the factory degrades to Noop, so this is
    safe even when NATS is down — operations just no-op / return None).
    """

    def __init__(
        self,
        adapter: EventBusAdapter,
        *,
        stream: str,
        subject_prefix: str,
        lane: str,
        queue_group: str,
        ack_wait_seconds: int,
        max_deliver: int,
    ) -> None:
        self._adapter = adapter
        self._stream = stream
        self._prefix = subject_prefix.rstrip(".")
        self._lane = lane
        self._queue_group = queue_group
        self._ack_wait = max(5, int(ack_wait_seconds))
        self._max_deliver = max(1, int(max_deliver))

    # ── subjects ────────────────────────────────────────────────────────

    @property
    def subject(self) -> str:
        """This service's lane subject — what it publishes to and consumes from."""
        return f"{self._prefix}.{self._lane}"

    @property
    def dlq_subject(self) -> str:
        return f"{self._prefix}.dlq"

    @property
    def max_deliver(self) -> int:
        return self._max_deliver

    @property
    def ack_wait_seconds(self) -> int:
        return self._ack_wait

    # ── lifecycle ───────────────────────────────────────────────────────

    async def ensure(self) -> None:
        """Declare the WorkQueue stream covering every lane + the DLQ subject.

        WorkQueue retention = a message is removed once any consumer acks it, so
        the stream is the live backlog, not an event log. Never raises — a stream
        failure is logged and dispatch falls back to the redis backend upstream.
        """
        try:
            await self._adapter.ensure_stream(
                self._stream,
                [f"{self._prefix}.*"],
                work_queue=True,
            )
        except Exception:  # noqa: BLE001 — caller decides whether to fall back
            logger.exception("dispatch: ensure_stream(%s) failed", self._stream)
            raise

    # ── produce ─────────────────────────────────────────────────────────

    async def enqueue(self, task_id: str) -> None:
        """Publish a run onto this lane. ``Nats-Msg-Id`` makes JetStream dedup
        a re-enqueue of the same run inside the stream's duplicate window, so a
        reconcile/retry can't double-dispatch."""
        await self._adapter.publish(
            self.subject,
            {"task_id": task_id},
            headers={"Nats-Msg-Id": task_id},
        )

    async def dead_letter(self, task_id: str, reason: str, attempts: int) -> None:
        """Park a poison run on the DLQ subject so an operator/reaper can see it."""
        with contextlib.suppress(Exception):
            await self._adapter.publish(
                self.dlq_subject,
                {"task_id": task_id, "reason": reason, "attempts": attempts, "lane": self._lane},
                headers={"Nats-Msg-Id": f"dlq-{task_id}"},
            )

    # ── consume ─────────────────────────────────────────────────────────

    async def subscribe(self, handler: MessageHandler, *, worker_index: int = 0) -> Subscription:
        """Attach a queue-group consumer. All workers (this pod + every other
        replica + the other service's pods on this lane) share delivery — one
        worker per message. ``durable_name`` is per-lane so the consumer survives
        restarts; ``queue_group`` load-balances across the pool."""
        return await self._adapter.subscribe(
            self.subject,
            handler,
            durable_name=f"pipeline-{self._lane}",
            queue_group=self._queue_group,
            manual_ack=True,
            max_deliver=self._max_deliver,
            ack_wait_seconds=self._ack_wait,
        )

    def is_final_attempt(self, msg: EventMessage) -> bool:
        """True when this delivery is the last the broker will make — the worker
        should dead-letter + ``term()`` instead of ``nack()`` (which would just
        bounce off max_deliver and drop the run with no record)."""
        return msg.num_delivered >= self._max_deliver

    @contextlib.asynccontextmanager
    async def keepalive(self, msg: EventMessage) -> AsyncIterator[None]:
        """Hold the message's ack deadline open for the duration of a long run by
        calling ``in_progress()`` every ``ack_wait/2``. The JetStream analog of
        the Redis claim heartbeat. A crashed worker stops the heartbeat → the
        broker redelivers after ack_wait."""
        interval = max(2.0, self._ack_wait / 2.0)

        async def _beat() -> None:
            while True:
                await asyncio.sleep(interval)
                with contextlib.suppress(Exception):
                    await msg.in_progress()

        hb = asyncio.create_task(_beat(), name="dispatch-keepalive")
        try:
            yield
        finally:
            hb.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await hb


__all__ = ["NatsWorkQueueBackend"]
