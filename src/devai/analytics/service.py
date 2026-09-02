"""AnalyticsService — pure aggregation over runtime + DB sources.

No FastAPI here — just functions that take the already-fetched task list (or a
DB pool) and return plain dicts the routes serialize. Kept separate so the
aggregation is unit-testable without standing up the whole app.

Timestamps in persisted tasks may be epoch floats (the runtime default) or ISO
strings (some persistence paths). `_epoch()` normalizes both, so the math is
robust to either shape.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

# Terminal task states and which count as success vs failure.
_SUCCESS_STATES = frozenset({"completed", "done", "succeeded"})
_FAILURE_STATES = frozenset({"failed", "stage_failed", "error", "cancelled", "stopped"})
_TERMINAL_STATES = _SUCCESS_STATES | _FAILURE_STATES


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
    return value if isinstance(value, list) else []


def _reference_names(value: Any) -> set[str]:
    values = value if isinstance(value, list) else [value]
    names: set[str] = set()
    for item in values:
        if isinstance(item, str) and item.strip():
            names.add(item.strip())
        elif isinstance(item, dict):
            name = str(item.get("ref") or item.get("name") or "").strip()
            if name:
                names.add(name)
    return names


def _run_artifacts(row: dict[str, Any]) -> set[tuple[str, str]]:
    configuration = _mapping(row.get("configuration"))
    spec = _mapping(_mapping(configuration.get("draft")).get("spec"))
    agent = str(row.get("agent") or _mapping(configuration.get("agent")).get("name") or "").strip()
    artifacts = {("agent", agent)} if agent else set()
    reference_fields = {
        "prompt": (spec.get("prompts"), spec.get("promptRef")),
        "skill": (spec.get("skills"), spec.get("skill")),
        "tool": (spec.get("tools"), spec.get("builtinTools")),
        "mcp_server": (spec.get("mcpServers"),),
    }
    for kind, groups in reference_fields.items():
        for group in groups:
            artifacts.update((kind, name) for name in _reference_names(group))
    prompt = _mapping(configuration.get("prompt"))
    prompt_name = str(prompt.get("ref") or prompt.get("name") or "").strip()
    if prompt_name:
        artifacts.add(("prompt", prompt_name))
    return artifacts


def summarize_lifecycle_evals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate durable sandbox evaluations and attribute them to pinned artifacts."""
    totals = {"runs": len(rows), "cases": 0, "passed": 0, "failed": 0, "tokens": 0, "cost_usd": 0.0}
    latencies: list[float] = []
    artifact_totals: dict[tuple[str, str], dict[str, Any]] = defaultdict(
        lambda: {"runs": 0, "cases": 0, "passed": 0, "cost_usd": 0.0, "tokens": 0, "agents": set()}
    )
    dimension_totals: dict[str, dict[str, float]] = defaultdict(
        lambda: {"average_sum": 0.0, "pass_rate_sum": 0.0, "runs": 0.0}
    )
    recent: list[dict[str, Any]] = []

    for row in rows:
        summary = _mapping(row.get("summary"))
        cases = int(summary.get("cases") or 0)
        passed = int(summary.get("passed") or 0)
        failed = int(summary.get("failed") or max(cases - passed, 0))
        tokens = int(summary.get("total_tokens") or 0)
        cost = float(summary.get("cost_usd") or 0.0)
        latency = float(summary.get("p95_latency_ms") or 0.0)
        totals["cases"] += cases
        totals["passed"] += passed
        totals["failed"] += failed
        totals["tokens"] += tokens
        totals["cost_usd"] += cost
        if latency:
            latencies.append(latency)

        agent = str(row.get("agent") or "")
        for artifact in _run_artifacts(row):
            aggregate = artifact_totals[artifact]
            aggregate["runs"] += 1
            aggregate["cases"] += cases
            aggregate["passed"] += passed
            aggregate["cost_usd"] += cost
            aggregate["tokens"] += tokens
            if agent:
                aggregate["agents"].add(agent)

        for name, dimension in _mapping(summary.get("dimensions")).items():
            values = _mapping(dimension)
            aggregate = dimension_totals[str(name)]
            aggregate["average_sum"] += float(values.get("average") or 0.0)
            aggregate["pass_rate_sum"] += float(values.get("pass_rate") or 0.0)
            aggregate["runs"] += 1

        recent.append(
            {
                "run_id": str(row.get("run_id") or row.get("id") or ""),
                "agent": agent,
                "suite": _mapping(row.get("suite")),
                "summary": summary,
                "failing_cases": _list(row.get("failing_cases")),
                "created_at": str(row.get("created_at") or ""),
            }
        )

    totals["pass_rate"] = round(totals["passed"] / totals["cases"], 4) if totals["cases"] else 0.0
    totals["cost_usd"] = round(float(totals["cost_usd"]), 6)
    totals["avg_p95_latency_ms"] = round(sum(latencies) / len(latencies), 1) if latencies else 0.0
    artifacts = []
    for (kind, name), aggregate in artifact_totals.items():
        artifacts.append(
            {
                "kind": kind,
                "name": name,
                "runs": aggregate["runs"],
                "cases": aggregate["cases"],
                "pass_rate": round(aggregate["passed"] / aggregate["cases"], 4) if aggregate["cases"] else 0.0,
                "cost_usd": round(aggregate["cost_usd"], 6),
                "tokens": aggregate["tokens"],
                "agents": sorted(aggregate["agents"]),
            }
        )
    dimensions = [
        {
            "name": name,
            "average": round(values["average_sum"] / values["runs"], 4),
            "pass_rate": round(values["pass_rate_sum"] / values["runs"], 4),
            "runs": int(values["runs"]),
        }
        for name, values in dimension_totals.items()
    ]
    return {
        "summary": totals,
        "artifacts": sorted(artifacts, key=lambda item: (item["kind"], item["name"])),
        "dimensions": sorted(dimensions, key=lambda item: item["name"]),
        "recent": recent[:50],
    }


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
    "summarize_lifecycle_evals",
    "summarize_runs",
]
