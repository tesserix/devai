"""AnalyticsService — pure aggregation over runtime + DB sources.

No FastAPI here — just functions that take the already-fetched task list (or a
DB pool) and return plain dicts the routes serialize. Kept separate so the
aggregation is unit-testable without standing up the whole app.

Timestamps in persisted tasks may be epoch floats (the runtime default) or ISO
strings (some persistence paths). `_epoch()` normalizes both, so the math is
robust to either shape.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

# Terminal task states and which count as success vs failure.
_SUCCESS_STATES = frozenset({"completed", "done", "succeeded"})
_FAILURE_STATES = frozenset({"failed", "stage_failed", "error", "cancelled", "stopped"})
_TERMINAL_STATES = _SUCCESS_STATES | _FAILURE_STATES


def _epoch(value: Any) -> float | None:
    """Normalize a timestamp (epoch float/int or ISO string) to epoch seconds."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            # Accept "2026-06-10T12:00:00Z" and "...+00:00".
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _day(value: Any) -> str | None:
    ep = _epoch(value)
    if ep is None:
        return None
    return datetime.fromtimestamp(ep, tz=UTC).strftime("%Y-%m-%d")


def _duration_ms(task: dict[str, Any]) -> float | None:
    """Wall-clock duration of a run, in ms, when both ends are known."""
    start = _epoch(task.get("started_at")) or _epoch(task.get("created_at"))
    end = _epoch(task.get("finished_at")) or _epoch(task.get("updated_at"))
    if start is None or end is None or end < start:
        return None
    return (end - start) * 1000.0


def summarize_runs(tasks: list[dict[str, Any]], *, window_days: int) -> dict[str, Any]:
    """Run-level KPIs + per-state / per-blueprint breakdowns."""
    by_state: dict[str, int] = defaultdict(int)
    by_blueprint: dict[str, dict[str, int]] = defaultdict(lambda: {"total": 0, "completed": 0, "failed": 0})
    durations: list[float] = []
    completed = failed = active = 0

    for t in tasks:
        state = str(t.get("state") or "unknown").lower()
        by_state[state] += 1
        bp = str(t.get("blueprint") or "unknown")
        by_blueprint[bp]["total"] += 1

        if state in _SUCCESS_STATES:
            completed += 1
            by_blueprint[bp]["completed"] += 1
        elif state in _FAILURE_STATES:
            failed += 1
            by_blueprint[bp]["failed"] += 1
        else:
            active += 1

        if state in _TERMINAL_STATES:
            d = _duration_ms(t)
            if d is not None:
                durations.append(d)

    terminal = completed + failed
    success_rate = round(completed / terminal, 4) if terminal else None
    avg_duration_ms = round(sum(durations) / len(durations), 1) if durations else None

    blueprint_rows = []
    for bp, c in sorted(by_blueprint.items(), key=lambda kv: kv[1]["total"], reverse=True):
        term = c["completed"] + c["failed"]
        blueprint_rows.append(
            {
                "blueprint": bp,
                "total": c["total"],
                "completed": c["completed"],
                "failed": c["failed"],
                "success_rate": round(c["completed"] / term, 4) if term else None,
            }
        )

    return {
        "window_days": window_days,
        "runs": {
            "total": len(tasks),
            "completed": completed,
            "failed": failed,
            "active": active,
            "success_rate": success_rate,
            "avg_duration_ms": avg_duration_ms,
        },
        "by_state": dict(by_state),
        "by_blueprint": blueprint_rows,
    }


def runs_timeseries(tasks: list[dict[str, Any]], *, days: int) -> list[dict[str, Any]]:
    """Runs/day bucketed by created_at, split by terminal status."""
    buckets: dict[str, dict[str, int]] = defaultdict(lambda: {"total": 0, "completed": 0, "failed": 0})
    for t in tasks:
        day = _day(t.get("created_at"))
        if day is None:
            continue
        state = str(t.get("state") or "").lower()
        buckets[day]["total"] += 1
        if state in _SUCCESS_STATES:
            buckets[day]["completed"] += 1
        elif state in _FAILURE_STATES:
            buckets[day]["failed"] += 1
    return [{"date": d, **buckets[d]} for d in sorted(buckets.keys())][-days:]


def stage_stats(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-stage run count, average duration, and failure count."""
    agg: dict[str, dict[str, float]] = defaultdict(lambda: {"runs": 0, "dur_sum": 0.0, "dur_n": 0, "failures": 0})
    for t in tasks:
        for ev in t.get("stage_events") or []:
            phase = str(ev.get("phase") or "").lower()
            if phase not in ("completed", "failed", "skipped"):
                continue
            stage = str(ev.get("stage") or "unknown")
            a = agg[stage]
            a["runs"] += 1
            if phase == "failed":
                a["failures"] += 1
            dur = ev.get("duration_ms")
            if isinstance(dur, (int, float)) and dur > 0:
                a["dur_sum"] += float(dur)
                a["dur_n"] += 1
    rows = []
    for stage, a in agg.items():
        rows.append(
            {
                "stage": stage,
                "runs": int(a["runs"]),
                "failures": int(a["failures"]),
                "avg_duration_ms": round(a["dur_sum"] / a["dur_n"], 1) if a["dur_n"] else None,
            }
        )
    return sorted(rows, key=lambda r: r["runs"], reverse=True)


__all__ = [
    "runs_timeseries",
    "stage_stats",
    "summarize_runs",
]
