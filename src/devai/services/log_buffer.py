"""In-process log ring buffer.

A `logging.Handler` that keeps the last N records in memory so the dashboard's
Logs page can show live application logs without any external store. Installed
once per process on the root logger (both FastAPI apps call `install()` at
startup); `/api/analytics/logs` reads it back with level/text filters.

This is deliberately NOT a log pipeline — durable storage is the GCS archive
(tesserix-k8s devai-log-archiver CronJob). The ring is the "what is the service
saying right now" view: bounded memory, zero I/O, survives nothing.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any

_DEFAULT_CAPACITY = 2000

_LEVEL_ORDER = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}


class RingBufferHandler(logging.Handler):
    """Keeps the last `capacity` formatted records in a thread-safe deque."""

    def __init__(self, capacity: int = _DEFAULT_CAPACITY) -> None:
        super().__init__(level=logging.INFO)
        self._records: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._lock_ = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = {
                "ts": record.created,
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage()[:2000],
            }
            if record.exc_info and record.exc_info[0] is not None:
                entry["exc"] = str(record.exc_info[1])[:500]
            with self._lock_:
                self._records.append(entry)
        except Exception:  # noqa: BLE001 — the log path must never raise
            pass

    def tail(
        self,
        *,
        limit: int = 200,
        min_level: str = "INFO",
        q: str = "",
        logger_prefix: str = "",
    ) -> list[dict[str, Any]]:
        """Most-recent-last slice with optional filters."""
        floor = _LEVEL_ORDER.get(min_level.upper(), 20)
        needle = q.lower()
        with self._lock_:
            snapshot = list(self._records)
        out = []
        for e in reversed(snapshot):  # newest first while filtering
            if _LEVEL_ORDER.get(e["level"], 0) < floor:
                continue
            if logger_prefix and not e["logger"].startswith(logger_prefix):
                continue
            if needle and needle not in e["message"].lower() and needle not in e["logger"].lower():
                continue
            out.append(e)
            if len(out) >= limit:
                break
        out.reverse()
        return out

    def stats(self) -> dict[str, Any]:
        with self._lock_:
            snapshot = list(self._records)
        counts: dict[str, int] = {}
        for e in snapshot:
            counts[e["level"]] = counts.get(e["level"], 0) + 1
        return {
            "buffered": len(snapshot),
            "capacity": self._records.maxlen,
            "by_level": counts,
            "oldest_ts": snapshot[0]["ts"] if snapshot else None,
            "now": time.time(),
        }


_installed: RingBufferHandler | None = None


def install(capacity: int = _DEFAULT_CAPACITY) -> RingBufferHandler:
    """Attach the ring to the root logger once; idempotent per process."""
    global _installed
    if _installed is None:
        _installed = RingBufferHandler(capacity)
        logging.getLogger().addHandler(_installed)
    return _installed


def get_buffer() -> RingBufferHandler | None:
    """The installed ring, or None when install() was never called."""
    return _installed


__all__ = ["RingBufferHandler", "get_buffer", "install"]
