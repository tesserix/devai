"""EventBusAdapter ABC + canonical message envelope.

The ABC declares the smallest surface every pub/sub backend must implement
so callers can swap nats ↔ redis_streams ↔ inproc with one env var.

`EventMessage` is the adapter-agnostic envelope passed to handlers. It
wraps the backend-native message and exposes `ack()`, `nack()`, `term()`
in a uniform way. Backends without explicit acks (e.g. in-process) treat
them as no-ops.

`Subscription` is the opaque handle returned by `subscribe()`. Callers
use it to `unsubscribe()` cleanly on shutdown.
"""

from __future__ import annotations

import json
import time
import uuid
from abc import abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from devai.adapters.base import Adapter


@dataclass(slots=True)
class EventMessage:
    """A single message — adapter-agnostic.

    The backend normalizes its native message shape (NATS Msg, Redis
    Streams entry, Kafka record, …) into this dataclass on the way in.
    Handlers receive ONLY this envelope.

    Ack methods are bound by the adapter when it dispatches; backends that
    don't support per-message acks set them to no-op coroutines.
    """

    subject: str
    data: bytes
    headers: dict[str, str] = field(default_factory=dict)
    seq: int | None = None
    reply_subject: str | None = None
    received_at: float = field(default_factory=time.time)
    message_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    provider: str = ""

    # Ack callbacks — adapter-bound. Default to no-op so a stage that
    # forgets to ack against a noop adapter doesn't break.
    _ack: Callable[[], Awaitable[None]] | None = None
    _nack: Callable[[], Awaitable[None]] | None = None
    _term: Callable[[], Awaitable[None]] | None = None

    async def ack(self) -> None:
        """Acknowledge — message is processed, don't redeliver."""
        if self._ack is not None:
            await self._ack()

    async def nack(self) -> None:
        """Negative-ack — redeliver after the backend's ack_wait."""
        if self._nack is not None:
            await self._nack()

    async def term(self) -> None:
        """Terminate — never redeliver (poison-pill handling)."""
        if self._term is not None:
            await self._term()

    def json(self) -> Any:
        """Decode payload as JSON. Raises if not JSON."""
        return json.loads(self.data.decode("utf-8"))

    def text(self) -> str:
        return self.data.decode("utf-8")


MessageHandler = Callable[[EventMessage], Awaitable[None]]


@dataclass(slots=True)
class Subscription:
    """Opaque handle returned by subscribe(). Used to unsubscribe.

    Backends fill `_native` with their concrete subscription object so
    they can cancel it on `unsubscribe()`. Callers only see this struct.
    """

    subject: str
    durable_name: str
    queue_group: str | None = None
    _native: Any = None
    _adapter: EventBusAdapter | None = None
    closed: bool = False

    async def unsubscribe(self) -> None:
        if self.closed or self._adapter is None:
            return
        await self._adapter._unsubscribe(self)
        self.closed = True


class EventBusAdapter(Adapter):
    """The minimum surface every event-bus backend implements.

    Six operations:

      - `connect`         — establish underlying connection / streams
      - `publish`         — fire a message at a subject
      - `subscribe`       — durable subscriber with a handler callback
      - `request`         — request/reply with a timeout
      - `ensure_stream`   — (JetStream-style) declare a stream up front
      - `close` + `health_check` — inherited Adapter contract

    Backends without native concepts (e.g. inproc has no streams) treat
    those methods as no-ops. The CALLER never branches on backend.
    """

    #: Set by subclass — used in logs and health output.
    provider_name: str = "abstract"

    # ──────────────────────────────────────────────────────────────────
    # Lifecycle
    # ──────────────────────────────────────────────────────────────────

    @abstractmethod
    async def connect(self) -> None:
        """Open the connection / build the JetStream context.

        Idempotent — calling twice should be a cheap no-op.
        """

    @abstractmethod
    async def ensure_stream(
        self,
        name: str,
        subjects: list[str],
        *,
        max_age_seconds: int = 7 * 24 * 3600,
        max_messages: int | None = None,
        max_bytes: int | None = None,
        work_queue: bool = True,
        replicas: int = 1,
    ) -> None:
        """Declare a stream / topic / channel up front.

        Backends without a stream concept (inproc, basic Redis pub/sub)
        accept the call and no-op. JetStream / Kafka / Streams use it.
        """

    # ──────────────────────────────────────────────────────────────────
    # Publish
    # ──────────────────────────────────────────────────────────────────

    @abstractmethod
    async def publish(
        self,
        subject: str,
        data: str | bytes | dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        """Publish a message to a subject.

        `data` accepts str (utf-8 encoded), bytes (raw), or dict
        (json-encoded). Subjects use NATS-style dotted notation
        (`devai.pipeline.task.created`); backends that don't support
        wildcards map them to their nearest equivalent.
        """

    # ──────────────────────────────────────────────────────────────────
    # Subscribe
    # ──────────────────────────────────────────────────────────────────

    @abstractmethod
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
        """Subscribe a handler to a subject.

        `durable_name` makes the subscription resumable across restarts
        (JetStream durable consumer). For backends without durables, it's
        used as a logical name.

        `queue_group` enables competing consumers — multiple subscribers
        in the same group share message delivery (load-balanced).
        """

    # Internal — called by Subscription.unsubscribe()
    @abstractmethod
    async def _unsubscribe(self, sub: Subscription) -> None: ...

    # ──────────────────────────────────────────────────────────────────
    # Request / reply (optional — used for synchronous lookups)
    # ──────────────────────────────────────────────────────────────────

    async def request(
        self,
        subject: str,
        data: str | bytes | dict[str, Any],
        *,
        timeout_seconds: float = 5.0,
    ) -> EventMessage | None:
        """Request/reply pattern. Default raises NotImplementedError.

        Backends that support it (NATS) override. Callers should treat
        a None return as "no responder available".
        """
        raise NotImplementedError(f"{self.provider_name} does not support request/reply")

    # ──────────────────────────────────────────────────────────────────
    # Adapter contract — defaults so subclasses can stay terse.
    # ──────────────────────────────────────────────────────────────────

    async def close(self) -> None:
        return None

    async def health_check(self) -> dict[str, Any]:
        return {
            "ok": True,
            "provider": self.provider_name,
            "detail": "no health check implemented",
        }


__all__ = [
    "EventBusAdapter",
    "EventMessage",
    "MessageHandler",
    "Subscription",
]
