from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from devai.orchestration.agent_lifecycle_outbox import AgentLifecycleOutboxRelay


class _Database:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.marked: list[tuple[str, datetime]] = []

    async def pending_agent_lifecycle_outbox(self, *, limit: int) -> list[dict[str, Any]]:
        assert limit == 100
        return list(self.rows)

    async def mark_agent_lifecycle_outbox_published(self, outbox_id: str, *, published_at: datetime) -> None:
        self.marked.append((outbox_id, published_at))

    async def agent_lifecycle_operational_snapshot(self) -> dict[str, float]:
        return {
            "live_sandboxes": 7.0,
            "pending_sandboxes": 2.0,
            "destroying_sandboxes": 1.0,
            "cleanup_backlog": 3.0,
            "stuck_workflows": 1.0,
            "outbox_pending": 4.0,
            "outbox_oldest_age_seconds": 12.0,
        }


class _Bus:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.published: list[tuple[str, dict[str, Any], dict[str, str]]] = []

    async def publish(
        self,
        subject: str,
        data: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        if self.fail:
            raise ConnectionError("event bus unavailable")
        self.published.append((subject, data, dict(headers or {})))


class _Telemetry:
    def __init__(self) -> None:
        self.counters: list[tuple[str, float, dict[str, str]]] = []
        self.observations: list[tuple[str, float, dict[str, str]]] = []
        self.gauges: list[tuple[str, float, dict[str, str]]] = []

    def incr(self, name: str, value: float = 1.0, attrs: dict[str, str] | None = None) -> None:
        self.counters.append((name, value, dict(attrs or {})))

    def observe(self, name: str, value: float, attrs: dict[str, str] | None = None) -> None:
        self.observations.append((name, value, dict(attrs or {})))

    def gauge(self, name: str, value: float, attrs: dict[str, str] | None = None) -> None:
        self.gauges.append((name, value, dict(attrs or {})))


def _row(now: datetime) -> dict[str, Any]:
    return {
        "id": "outbox-1",
        "event_id": "event-1",
        "event_type": "agent_lifecycle.transitioned",
        "tenant_id": "acme",
        "payload": {
            "event_id": "event-1",
            "workflow_id": "agent-eval:acme:agent-lab:digest",
            "sequence": 3,
            "operation": "evaluate",
            "state": "succeeded",
            "step": "complete",
            "error_code": "",
        },
        "created_at": now - timedelta(seconds=12),
    }


@pytest.mark.asyncio
async def test_relay_publishes_with_deduplication_metadata_before_marking_the_row() -> None:
    now = datetime(2026, 8, 29, 0, 0, tzinfo=UTC)
    database = _Database([_row(now)])
    bus = _Bus()
    telemetry = _Telemetry()
    relay = AgentLifecycleOutboxRelay(database, bus, telemetry=telemetry, clock=lambda: now)

    published = await relay.relay_once()

    assert published == 1
    assert bus.published == [
        (
            "devai.agent.lifecycle.transitioned",
            {**_row(now)["payload"], "tenant_id": "acme"},
            {"Nats-Msg-Id": "event-1", "Idempotency-Key": "event-1"},
        )
    ]
    assert database.marked == [("outbox-1", now)]
    assert telemetry.observations == [
        (
            "devai.agent.lifecycle.outbox.queue_age_seconds",
            12.0,
            {"operation": "evaluate", "state": "succeeded"},
        )
    ]
    assert (
        "devai.agent.lifecycle.stuck_workflows",
        1.0,
        {},
    ) in telemetry.gauges
    assert ("devai.sandbox.live", 7.0, {}) in telemetry.gauges
    assert ("devai.sandbox.cleanup_backlog", 3.0, {}) in telemetry.gauges


@pytest.mark.asyncio
async def test_relay_leaves_the_row_unpublished_when_the_event_bus_is_unavailable() -> None:
    now = datetime(2026, 8, 29, 0, 0, tzinfo=UTC)
    database = _Database([_row(now)])
    telemetry = _Telemetry()
    relay = AgentLifecycleOutboxRelay(database, _Bus(fail=True), telemetry=telemetry, clock=lambda: now)

    published = await relay.relay_once()

    assert published == 0
    assert database.marked == []
    assert (
        "devai.agent.lifecycle.outbox.publish_failures",
        1.0,
        {"event_type": "agent_lifecycle.transitioned"},
    ) in telemetry.counters
