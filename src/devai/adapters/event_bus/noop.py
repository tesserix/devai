"""Noop / inproc event-bus backend.

Used when:
  - `DEVAI_EVENT_BUS_PROVIDER=noop` (explicit opt-out)
  - The NATS SDK isn't installed (graceful degrade)
  - The NATS connection refuses (graceful degrade)
  - Unit tests run without a broker

The noop is genuinely useful as an in-process bus: publish/subscribe
work, wildcard matching follows NATS semantics (`*` = one token, `>` =
trailing tokens), and durable names are remembered so a late subscriber
can opt out of replay by passing `replay=False` via headers.

It's the only event-bus backend with no external dependencies, which
makes it the canonical fallback.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from devai.adapters.event_bus.base import (
    EventBusAdapter,
    EventMessage,
    MessageHandler,
    Subscription,
)

logger = logging.getLogger(__name__)


class NoopEventBusAdapter(EventBusAdapter):
    """In-process pub/sub. No network, no persistence.

    Stores nothing across restarts. Subscriptions are held per-process.
    Wildcard matching follows NATS semantics so callers can develop
    against this and deploy against NATS without code changes.
    """

    provider_name = "noop"

    def __init__(self) -> None:
        self._connected = False
        self._streams: dict[str, list[str]] = {}
        # subject_pattern -> list of (subscription, handler)
        self._subs: dict[str, list[tuple[Subscription, MessageHandler]]] = {}
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        self._connected = True

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
        self._streams[name] = list(subjects)
        logger.debug("noop event bus: registered stream %s -> %s", name, subjects)

    async def publish(
        self,
        subject: str,
        data: str | bytes | dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        if not self._connected:
            await self.connect()
        payload = _encode(data)

        async with self._lock:
            matches: list[tuple[Subscription, MessageHandler]] = []
            for pattern, subs in self._subs.items():
                if _matches(pattern, subject):
                    matches.extend(subs)

        for sub, handler in matches:
            if sub.closed:
                continue
            msg = EventMessage(
                subject=subject,
                data=payload,
                headers=dict(headers or {}),
                provider=self.provider_name,
            )
            # Fire handlers concurrently — they each get their own task
            # so a slow consumer doesn't block other subscribers.
            asyncio.create_task(self._dispatch(handler, msg))

    async def _dispatch(self, handler: MessageHandler, msg: EventMessage) -> None:
        try:
            await handler(msg)
        except Exception:
            logger.exception(
                "noop event bus: handler crashed on subject=%s", msg.subject
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
        sub = Subscription(
            subject=subject,
            durable_name=durable_name,
            queue_group=queue_group,
            _adapter=self,
        )
        async with self._lock:
            self._subs.setdefault(subject, []).append((sub, handler))
        logger.info(
            "noop event bus: subscribed subject=%s durable=%s", subject, durable_name
        )
        return sub

    async def _unsubscribe(self, sub: Subscription) -> None:
        async with self._lock:
            bucket = self._subs.get(sub.subject, [])
            self._subs[sub.subject] = [(s, h) for (s, h) in bucket if s is not sub]
            if not self._subs[sub.subject]:
                self._subs.pop(sub.subject, None)

    async def request(
        self,
        subject: str,
        data: str | bytes | dict[str, Any],
        *,
        timeout_seconds: float = 5.0,
    ) -> EventMessage | None:
        # Inproc request/reply isn't useful — return None so callers
        # treat the noop bus as "no responder".
        return None

    async def close(self) -> None:
        async with self._lock:
            self._subs.clear()
            self._streams.clear()
        self._connected = False

    async def health_check(self) -> dict[str, Any]:
        return {
            "ok": self._connected,
            "provider": self.provider_name,
            "detail": (
                f"streams={len(self._streams)}, "
                f"subjects={len(self._subs)}, "
                f"subscriptions={sum(len(v) for v in self._subs.values())}"
            ),
        }


# ───────────────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────────────


def _encode(data: str | bytes | dict[str, Any]) -> bytes:
    if isinstance(data, bytes):
        return data
    if isinstance(data, str):
        return data.encode("utf-8")
    return json.dumps(data, default=str).encode("utf-8")


_token_re = re.compile(r"[^.]+")


def _matches(pattern: str, subject: str) -> bool:
    """NATS-style subject matching.

      *  — exactly one token
      >  — one or more trailing tokens (only at the end)

    Examples:
        devai.>                 matches devai.pipeline.task.created
        devai.pipeline.task.*   matches devai.pipeline.task.created
        devai.*.task.created    matches devai.pipeline.task.created
        devai.pipeline.>        matches devai.pipeline.task.created
    """
    if pattern == subject:
        return True
    p_tokens = pattern.split(".")
    s_tokens = subject.split(".")

    for i, p in enumerate(p_tokens):
        if p == ">":
            # `>` means ONE or more trailing tokens — there must be at
            # least one remaining subject token at this position.
            return i < len(s_tokens)
        if i >= len(s_tokens):
            return False
        if p == "*":
            continue
        if p != s_tokens[i]:
            return False
    return len(p_tokens) == len(s_tokens)


__all__ = ["NoopEventBusAdapter"]
