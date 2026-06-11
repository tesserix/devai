"""Factory — pick the right event-bus backend at runtime.

Reads `settings.event_bus_provider` (env: `DEVAI_EVENT_BUS_PROVIDER`)
and returns one of:

    noop  → NoopEventBusAdapter   (in-process loopback)
    nats  → NatsEventBusAdapter   (JetStream)

Graceful degradation rules (identical to adapters/memory):

  - Unknown provider name → Noop + warning
  - nats-py SDK missing   → Noop + warning
  - NATS connect refused  → caller decides (see create_and_connect_event_bus)

The factory itself NEVER raises — the pod must degrade, not crash.
"""

from __future__ import annotations

import logging
from typing import Any

from devai.adapters.base import (
    AdapterError,
    AdapterNotConfigured,
    AdapterNotInstalled,
    AdapterRegistry,
)
from devai.adapters.event_bus.base import EventBusAdapter
from devai.adapters.event_bus.noop import NoopEventBusAdapter

logger = logging.getLogger(__name__)

KNOWN_PROVIDERS = ("noop", "nats")


def _build_noop(settings: Any) -> EventBusAdapter:  # noqa: ARG001
    return NoopEventBusAdapter()


def _build_nats(settings: Any) -> EventBusAdapter:
    from devai.adapters.event_bus.nats_adapter import NatsEventBusAdapter

    url = getattr(settings, "nats_url", "") or ""
    if not url:
        raise AdapterNotConfigured("nats event-bus adapter requires DEVAI_NATS_URL")
    return NatsEventBusAdapter(
        url=url,
        default_stream=getattr(settings, "nats_stream", "DEVAI"),
        default_max_deliver=getattr(settings, "nats_max_deliver", 3),
        default_ack_wait_seconds=getattr(settings, "nats_ack_wait", 300),
        default_work_queue=bool(getattr(settings, "nats_stream_work_queue", False)),
    )


event_bus_registry: AdapterRegistry[EventBusAdapter] = AdapterRegistry("event_bus")
event_bus_registry.register("noop", _build_noop)
event_bus_registry.register("nats", _build_nats)


def create_event_bus_adapter(settings: Any) -> EventBusAdapter:
    """Resolve `settings.event_bus_provider` to a constructed adapter.

    Never raises. On any error returns a NoopEventBusAdapter and logs the
    reason. The caller is responsible for calling `await adapter.connect()`
    afterwards (the factory deliberately doesn't do I/O).
    """
    provider = (getattr(settings, "event_bus_provider", "nats") or "nats").lower()
    if not event_bus_registry.has(provider):
        logger.warning(
            "event_bus_provider=%r is unknown (known: %s) — using Noop",
            provider,
            ", ".join(event_bus_registry.known()),
        )
        return NoopEventBusAdapter()

    try:
        adapter = event_bus_registry.resolve(provider, settings)
        logger.info("EventBusAdapter active: %s", adapter.provider_name)
        return adapter
    except AdapterNotInstalled as e:
        logger.warning("event_bus_provider=%s: %s — using Noop", provider, e)
        return NoopEventBusAdapter()
    except AdapterNotConfigured as e:
        logger.warning("event_bus_provider=%s: %s — using Noop", provider, e)
        return NoopEventBusAdapter()
    except AdapterError as e:
        logger.warning("event_bus_provider=%s failed to build (%s) — using Noop", provider, e)
        return NoopEventBusAdapter()
    except Exception:  # noqa: BLE001
        logger.exception("event_bus_provider=%s crashed during build — using Noop", provider)
        return NoopEventBusAdapter()


async def create_and_connect_event_bus(settings: Any) -> EventBusAdapter:
    """Convenience: build + connect, falling back to Noop on connect failure.

    Use this anywhere you need a ready-to-publish adapter. The caller
    still owns close() on shutdown.
    """
    adapter = create_event_bus_adapter(settings)
    try:
        await adapter.connect()
    except Exception:
        logger.exception(
            "event_bus connect failed (%s) — falling back to Noop in-process bus",
            adapter.provider_name,
        )
        # Replace with Noop so dependent code still publishes successfully
        # (into the void). Callers that need real durability should check
        # health_check() during readiness.
        adapter = NoopEventBusAdapter()
        await adapter.connect()
    return adapter


__all__ = [
    "KNOWN_PROVIDERS",
    "create_event_bus_adapter",
    "create_and_connect_event_bus",
    "event_bus_registry",
]
