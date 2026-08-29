"""At-least-once delivery for durable Agent lifecycle transitions."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

_SUBJECT = "devai.agent.lifecycle.transitioned"


class AgentLifecycleOutboxRelay:
    """Publish committed lifecycle intents and acknowledge them afterwards."""

    def __init__(
        self,
        database: Any,
        event_bus: Any,
        *,
        telemetry: Any,
        clock: Callable[[], datetime] | None = None,
        batch_size: int = 100,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        self._database = database
        self._event_bus = event_bus
        self._telemetry = telemetry
        self._clock = clock or (lambda: datetime.now(UTC))
        self._batch_size = max(1, batch_size)
        self._poll_interval_seconds = max(0.1, poll_interval_seconds)

    async def relay_once(self) -> int:
        await self._record_operational_snapshot()
        rows = await self._database.pending_agent_lifecycle_outbox(limit=self._batch_size)
        published = 0
        for row in rows:
            payload = dict(row["payload"])
            tenant_id = str(row.get("tenant_id") or "")
            if tenant_id:
                payload["tenant_id"] = tenant_id
            event_id = str(row["event_id"])
            attrs = {
                "operation": str(payload.get("operation") or "unknown"),
                "state": str(payload.get("state") or "unknown"),
            }
            now = self._clock()
            age = max(0.0, (now - row["created_at"]).total_seconds())
            self._telemetry.observe(
                "devai.agent.lifecycle.outbox.queue_age_seconds",
                age,
                attrs,
            )
            try:
                await self._event_bus.publish(
                    _SUBJECT,
                    payload,
                    headers={"Nats-Msg-Id": event_id, "Idempotency-Key": event_id},
                )
            except Exception:  # noqa: BLE001 - leave intent pending for the next relay pass
                self._telemetry.incr(
                    "devai.agent.lifecycle.outbox.publish_failures",
                    attrs={"event_type": str(row["event_type"])},
                )
                logger.exception("agent lifecycle outbox publish failed")
                break
            await self._database.mark_agent_lifecycle_outbox_published(
                str(row["id"]),
                published_at=now,
            )
            self._telemetry.incr("devai.agent.lifecycle.outbox.published", attrs=attrs)
            published += 1
        return published

    async def _record_operational_snapshot(self) -> None:
        try:
            snapshot = await self._database.agent_lifecycle_operational_snapshot()
        except Exception:  # noqa: BLE001 - metrics do not gate event delivery
            self._telemetry.incr("devai.agent.lifecycle.snapshot_failures")
            logger.debug("agent lifecycle operational snapshot failed", exc_info=True)
            return
        names = {
            "live_sandboxes": "devai.sandbox.live",
            "pending_sandboxes": "devai.sandbox.pending",
            "destroying_sandboxes": "devai.sandbox.destroying",
            "cleanup_backlog": "devai.sandbox.cleanup_backlog",
            "stuck_workflows": "devai.agent.lifecycle.stuck_workflows",
            "outbox_pending": "devai.agent.lifecycle.outbox.pending",
            "outbox_oldest_age_seconds": "devai.agent.lifecycle.outbox.oldest_age_seconds",
        }
        for key, metric in names.items():
            self._telemetry.gauge(metric, float(snapshot.get(key, 0.0)))

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                published = await self.relay_once()
            except Exception:  # noqa: BLE001 - the worker remains available while Postgres recovers
                self._telemetry.incr("devai.agent.lifecycle.outbox.poll_failures")
                logger.exception("agent lifecycle outbox poll failed")
                published = 0
            if published >= self._batch_size:
                continue
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._poll_interval_seconds)
            except TimeoutError:
                continue


__all__ = ["AgentLifecycleOutboxRelay"]
