"""NATS JetStream event bus for inter-agent communication."""

from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any

import nats
from nats.js.api import (
    AckPolicy,
    ConsumerConfig,
    DeliverPolicy,
    RetentionPolicy,
    StorageType,
    StreamConfig,
)

if TYPE_CHECKING:
    from nats.aio.client import Client as NATSClient
    from nats.aio.msg import Msg
    from nats.js.client import JetStreamContext

logger = logging.getLogger(__name__)

MessageHandler = Callable[["Msg"], Coroutine[Any, Any, None]]


class EventBus:
    """Wraps NATS JetStream for pipeline event publishing and subscribing."""

    def __init__(
        self,
        stream_name: str = "DEVAI",
        max_deliver: int = 3,
        ack_wait: int = 300,
    ) -> None:
        self.stream_name = stream_name
        self.max_deliver = max_deliver
        self.ack_wait = ack_wait
        self._nc: NATSClient | None = None
        self._js: JetStreamContext | None = None

    @property
    def js(self) -> JetStreamContext:
        if self._js is None:
            raise RuntimeError("EventBus not connected. Call connect() first.")
        return self._js

    async def connect(self, nats_url: str) -> None:
        """Connect to NATS and ensure the DEVAI stream exists."""
        self._nc = await nats.connect(nats_url)
        self._js = self._nc.jetstream()

        stream_config = StreamConfig(
            name=self.stream_name,
            subjects=["devai.>"],
            retention=RetentionPolicy.WORK_QUEUE,
            max_age=7 * 24 * 3600,  # 7 days in seconds
            storage=StorageType.FILE,
            num_replicas=1,
        )

        try:
            await self._js.find_stream_name_by_subject("devai.pipeline.trigger")
            await self._js.update_stream(stream_config)
            logger.info("Updated existing NATS stream: %s", self.stream_name)
        except nats.js.errors.NotFoundError:
            await self._js.add_stream(stream_config)
            logger.info("Created NATS stream: %s", self.stream_name)

    async def publish(self, subject: str, data: str | bytes) -> None:
        """Publish a message to a NATS subject."""
        if isinstance(data, str):
            data = data.encode("utf-8")
        ack = await self.js.publish(subject, data)
        logger.debug("Published to %s (seq=%d)", subject, ack.seq)

    async def subscribe(
        self,
        subject: str,
        handler: MessageHandler,
        durable_name: str,
    ) -> None:
        """Subscribe to a subject with a durable consumer."""
        consumer_config = ConsumerConfig(
            ack_policy=AckPolicy.EXPLICIT,
            deliver_policy=DeliverPolicy.ALL,
            max_deliver=self.max_deliver,
            ack_wait=self.ack_wait,
        )

        await self.js.subscribe(
            subject,
            cb=handler,
            durable=durable_name,
            manual_ack=True,
            config=consumer_config,
        )
        logger.info("Subscribed to %s (durable=%s)", subject, durable_name)

    async def close(self) -> None:
        """Close the NATS connection."""
        if self._nc and not self._nc.is_closed:
            await self._nc.drain()
            await self._nc.close()
            logger.info("NATS connection closed")
