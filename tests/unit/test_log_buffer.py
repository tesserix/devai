"""Log ring buffer — capture, filtering, stats."""

from __future__ import annotations

import logging

from devai.services.log_buffer import RingBufferHandler


def _make(capacity: int = 50) -> tuple[logging.Logger, RingBufferHandler]:
    handler = RingBufferHandler(capacity)
    log = logging.getLogger(f"test.ring.{id(handler)}")
    log.setLevel(logging.DEBUG)
    log.addHandler(handler)
    log.propagate = False
    return log, handler


def test_captures_and_tails_newest_last() -> None:
    log, ring = _make()
    log.info("first")
    log.warning("second")
    entries = ring.tail(limit=10)
    assert [e["message"] for e in entries] == ["first", "second"]
    assert entries[1]["level"] == "WARNING"


def test_level_floor_and_text_filter() -> None:
    log, ring = _make()
    log.info("hello world")
    log.error("boom in service")
    assert [e["message"] for e in ring.tail(min_level="ERROR")] == ["boom in service"]
    assert [e["message"] for e in ring.tail(q="hello")] == ["hello world"]
    assert ring.tail(q="absent") == []


def test_capacity_bound_and_stats() -> None:
    log, ring = _make(capacity=5)
    for i in range(20):
        log.info("m%d", i)
    entries = ring.tail(limit=100)
    assert len(entries) == 5
    assert entries[-1]["message"] == "m19"
    stats = ring.stats()
    assert stats["buffered"] == 5
    assert stats["capacity"] == 5
    assert stats["by_level"]["INFO"] == 5


def test_handler_never_raises_on_bad_record() -> None:
    _, ring = _make()
    # A record whose msg formatting would explode — emit must swallow it.
    bad = logging.LogRecord("x", logging.INFO, "f", 1, "%d", ("not-an-int",), None)
    ring.emit(bad)  # no exception
