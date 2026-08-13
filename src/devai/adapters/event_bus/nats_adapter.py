"""NATS JetStream event-bus backend.

Wraps `nats-py` so the rest of DevAI talks to a stable interface. The
SDK is lazy-imported inside __init__ so this module is safe to import
even when nats-py isn't installed — the factory will catch the
ImportError and degrade to Noop.

Stream defaults:

  - name        — `DEVAI` (overridable via settings.nats_stream)
  - subjects    — `devai.>` (every devai.* topic ends up in the stream)
  - retention   — WORK_QUEUE so consumed messages don't replay to new subs
  - storage     — FILE so survives broker restarts
  - max_age     — 7 days

Per-subscription defaults (overridable per call):

  - ack_policy  — EXPLICIT
  - max_deliver — 3 (then dead-letter)
  - ack_wait    — 300s
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from devai.adapters.base import AdapterError, AdapterNotInstalled
from devai.adapters.event_bus.base import (
    EventBusAdapter,
    EventMessage,
    MessageHandler,
    Subscription,
)
from devai.adapters.event_bus.noop import _encode

if TYPE_CHECKING:
    from nats.aio.client import Client as NATSClient
    from nats.aio.msg import Msg
    from nats.js.client import JetStreamContext

logger = logging.getLogger(__name__)


class NatsEventBusAdapter(EventBusAdapter):
    """JetStream-backed event bus."""

    provider_name = "nats"

    def __init__(
        self,
        url: str,
        *,
        creds: str = "",
        default_stream: str = "DEVAI",
        default_subjects: tuple[str, ...] = ("devai.>",),
        default_max_deliver: int = 3,
        default_ack_wait_seconds: int = 300,
        default_max_age_seconds: int = 7 * 24 * 3600,
        default_replicas: int = 1,
        default_work_queue: bool = False,
    ) -> None:
        # Lazy import — adapter is constructible even without nats-py;
        # connect() is where we materialize the client.
        try:
            import nats  # noqa: F401
        except ImportError as e:
            raise AdapterNotInstalled("nats-py is not installed (`pip install nats-py`)") from e

        self.url = url
        # Path to a NATS account .creds file for operator/JWT auth. Empty →
        # anonymous connect (default), so this is inert until the cluster
        # enforces NATS auth and the chart mounts the creds file.
        self.creds = creds or ""
        self.default_stream = default_stream
        self.default_subjects = list(default_subjects)
        self.default_max_deliver = default_max_deliver
        self.default_ack_wait_seconds = default_ack_wait_seconds
        self.default_max_age_seconds = default_max_age_seconds
        self.default_replicas = default_replicas
        # LIMITS (False) is the right default for an observability fan-out
        # bus: any number of consumers can read the same subjects and
        # messages age out by the limits. WORK_QUEUE restricts overlapping
        # consumers and assumes exactly-once consumption — only correct for
        # actual job queues; opt back in via DEVAI_NATS_STREAM_WORK_QUEUE.
        self.default_work_queue = default_work_queue

        self._nc: NATSClient | None = None
        self._js: JetStreamContext | None = None
        self._ensured_streams: set[str] = set()
        self._subs: list[tuple[Subscription, Any]] = []

    # ──────────────────────────────────────────────────────────────────
    # Lifecycle
    # ──────────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        if self._nc is not None and not self._nc.is_closed:
            return
        import nats

        # Pass user credentials only when a .creds path is configured; otherwise
        # connect anonymously exactly as before (no behaviour change).
        connect_kwargs: dict[str, Any] = {}
        if self.creds:
            connect_kwargs["user_credentials"] = self.creds
        try:
            self._nc = await nats.connect(self.url, **connect_kwargs)
        except Exception as e:
            raise AdapterError(f"NATS connect failed for {self.url}: {e}") from e
        self._js = self._nc.jetstream()
        logger.info("NATS adapter connected to %s", self.url)

        # Auto-declare the default stream so `publish` works without an
        # explicit ensure_stream call. Tests / niche callers can override
        # by calling ensure_stream() with different subjects.
        try:
            await self.ensure_stream(
                self.default_stream,
                self.default_subjects,
                max_age_seconds=self.default_max_age_seconds,
                replicas=self.default_replicas,
                work_queue=self.default_work_queue,
            )
        except Exception:
            logger.exception(
                "NATS adapter: default stream %s setup failed — publish will fail until repaired",
                self.default_stream,
            )

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
        if self._js is None:
            await self.connect()
        assert self._js is not None  # for type-checker

        import nats
        from nats.js.api import RetentionPolicy, StorageType, StreamConfig

        retention = RetentionPolicy.WORK_QUEUE if work_queue else RetentionPolicy.LIMITS

        cfg_kwargs: dict[str, Any] = {
            "name": name,
            "subjects": list(subjects),
            "retention": retention,
            "max_age": max_age_seconds,
            "storage": StorageType.FILE,
            "num_replicas": replicas,
        }
        if max_messages is not None:
            cfg_kwargs["max_msgs"] = max_messages
        if max_bytes is not None:
            cfg_kwargs["max_bytes"] = max_bytes

        cfg = StreamConfig(**cfg_kwargs)

        try:
            info = await self._js.stream_info(name)
            try:
                await self._js.update_stream(cfg)
                logger.info("NATS adapter: updated stream %s", name)
            except Exception as e:  # noqa: BLE001
                # JetStream cannot change retention policy in place. Never
                # delete+recreate here (that would drop retained messages) —
                # keep the existing stream and tell the operator how to
                # migrate (delete the stream once; it is recreated on the
                # next connect with the new retention).
                existing = getattr(getattr(info, "config", None), "retention", None)
                if existing is not None and existing != retention:
                    logger.warning(
                        "NATS adapter: stream %s retention is %s, want %s — retention cannot be "
                        "changed in place. Delete the stream (nats stream rm %s) to migrate; "
                        "continuing with the existing config. (%s)",
                        name,
                        existing,
                        retention,
                        name,
                        e,
                    )
                else:
                    raise
        except nats.js.errors.NotFoundError:
            await self._js.add_stream(cfg)
            logger.info("NATS adapter: created stream %s with subjects=%s", name, subjects)

        self._ensured_streams.add(name)

    # ──────────────────────────────────────────────────────────────────
    # Publish / subscribe / request
    # ──────────────────────────────────────────────────────────────────

    async def publish(
        self,
        subject: str,
        data: str | bytes | dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        if self._js is None:
            await self.connect()
        assert self._js is not None

        payload = _encode(data)
        ack = await self._js.publish(subject, payload, headers=headers)
        logger.debug(
            "NATS adapter: published subject=%s seq=%s stream=%s",
            subject,
            getattr(ack, "seq", None),
            getattr(ack, "stream", None),
        )

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
        if self._js is None:
            await self.connect()
        assert self._js is not None

        from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy

        cfg = ConsumerConfig(
            ack_policy=AckPolicy.EXPLICIT if manual_ack else AckPolicy.NONE,
            deliver_policy=DeliverPolicy.ALL,
            max_deliver=max_deliver or self.default_max_deliver,
            ack_wait=ack_wait_seconds or self.default_ack_wait_seconds,
        )

        wrapped = self._wrap_handler(handler)

        native = await self._js.subscribe(
            subject,
            cb=wrapped,
            durable=durable_name,
            queue=queue_group,
            manual_ack=manual_ack,
            config=cfg,
        )

        sub = Subscription(
            subject=subject,
            durable_name=durable_name,
            queue_group=queue_group,
            _native=native,
            _adapter=self,
        )
        self._subs.append((sub, native))
        logger.info(
            "NATS adapter: subscribed subject=%s durable=%s queue=%s",
            subject,
            durable_name,
            queue_group,
        )
        return sub

    async def _unsubscribe(self, sub: Subscription) -> None:
        native = sub._native
        if native is None:
            return
        try:
            await native.unsubscribe()
        except Exception:
            logger.exception("NATS adapter: unsubscribe failed for %s", sub.subject)
        self._subs = [(s, n) for (s, n) in self._subs if s is not sub]

    async def request(
        self,
        subject: str,
        data: str | bytes | dict[str, Any],
        *,
        timeout_seconds: float = 5.0,
    ) -> EventMessage | None:
        if self._nc is None or self._nc.is_closed:
            await self.connect()
        assert self._nc is not None

        payload = _encode(data)
        try:
            msg = await self._nc.request(subject, payload, timeout=timeout_seconds)
        except Exception as e:
            logger.warning("NATS adapter: request to %s timed out: %s", subject, e)
            return None

        return EventMessage(
            subject=msg.subject,
            data=msg.data or b"",
            headers=dict(msg.headers or {}),
            reply_subject=getattr(msg, "reply", None),
            provider=self.provider_name,
        )

    # ──────────────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────────────

    def _wrap_handler(self, handler: MessageHandler) -> Any:
        """Translate a NATS Msg into our EventMessage on dispatch."""

        async def _cb(msg: Msg) -> None:  # noqa: F821
            meta = getattr(msg, "metadata", None)
            envelope = EventMessage(
                subject=msg.subject,
                data=msg.data or b"",
                headers=dict(msg.headers or {}),
                seq=getattr(meta, "sequence", None),
                num_delivered=int(getattr(meta, "num_delivered", 1) or 1),
                reply_subject=getattr(msg, "reply", None),
                provider=self.provider_name,
            )
            envelope._ack = msg.ack
            envelope._nack = lambda: msg.nak()  # type: ignore[attr-defined]
            envelope._term = lambda: msg.term()  # type: ignore[attr-defined]
            envelope._in_progress = lambda: msg.in_progress()  # type: ignore[attr-defined]
            try:
                await handler(envelope)
            except Exception:
                logger.exception(
                    "NATS adapter: handler crashed on subject=%s — sending nack",
                    msg.subject,
                )
                try:
                    await msg.nak()
                except Exception:
                    pass

        return _cb

    # ──────────────────────────────────────────────────────────────────
    # Adapter contract
    # ──────────────────────────────────────────────────────────────────

    async def close(self) -> None:
        if self._nc is not None and not self._nc.is_closed:
            try:
                await self._nc.drain()
            except Exception:
                logger.exception("NATS adapter: drain failed")
            try:
                await self._nc.close()
            except Exception:
                logger.exception("NATS adapter: close failed")
        self._nc = None
        self._js = None
        self._subs.clear()

    async def health_check(self) -> dict[str, Any]:
        if self._nc is None:
            return {
                "ok": False,
                "provider": self.provider_name,
                "detail": "not connected",
            }
        try:
            connected = not self._nc.is_closed
        except Exception as e:
            return {
                "ok": False,
                "provider": self.provider_name,
                "detail": f"status check failed: {e}",
            }
        return {
            "ok": connected,
            "provider": self.provider_name,
            "detail": (f"url={self.url}, streams={sorted(self._ensured_streams)}, subscriptions={len(self._subs)}"),
        }


__all__ = ["NatsEventBusAdapter"]
