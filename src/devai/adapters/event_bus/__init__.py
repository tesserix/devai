"""Event-bus adapter family — pluggable pub/sub backends.

Public surface:

    EventBusAdapter                 — ABC every backend implements
    EventMessage                    — adapter-agnostic envelope
    Subscription                    — handle returned by subscribe()
    create_event_bus_adapter(settings) — factory; reads DEVAI_EVENT_BUS_PROVIDER

Backends:

    noop  — in-process loopback; tests + disabled mode
    nats  — NATS JetStream (durable, work-queue retention)

Future drop-ins (the factory + registry don't need to change to add them):

    redis_streams, inproc, kafka, google_pubsub

Backend SDKs are lazy-imported.
"""

from devai.adapters.event_bus.base import (
    EventBusAdapter,
    EventMessage,
    Subscription,
)
from devai.adapters.event_bus.factory import (
    KNOWN_PROVIDERS,
    create_and_connect_event_bus,
    create_event_bus_adapter,
    event_bus_registry,
)

__all__ = [
    "KNOWN_PROVIDERS",
    "EventBusAdapter",
    "EventMessage",
    "Subscription",
    "create_and_connect_event_bus",
    "create_event_bus_adapter",
    "event_bus_registry",
]
