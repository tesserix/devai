"""FastAPI routes for the analytics surface.

Mounted at `/api/analytics/*` by `devai.webhook.app.create_app`. Read-only.

Sources, in order of reliability:
  - pipeline runtime (Redis-persisted Fiber tasks) — run/stage stats; the
    source of truth for the active runtime.
  - Postgres `agent_executions` — per-agent / per-LLM token + cost rollups.
  - shared `devai_db` SRE tables — best-effort SRE summary strip.
  - telemetry adapter + Prometheus — OTel/collector health panel.

Every endpoint degrades to empty/null sections rather than 5xx when a source
is unavailable, so the page always renders.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi import APIRouter, Query, Request

from devai.analytics.service import runs_timeseries, stage_stats, summarize_runs

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/analytics", tags=["analytics"])

# Pull a generous window of persisted tasks for aggregation. The runtime caps
# its own retention (pipeline_task_ttl), so this is the working set, not all
# history-of-time.
_TASK_FETCH_LIMIT = 1000


async def _tasks(request: Request, *, blueprint: str | None = None, repo: str | None = None) -> list[dict[str, Any]]:
    """Fetch persisted pipeline tasks; [] when the runtime is disabled."""
    svc = getattr(request.app.state, "pipeline_service", None)
    if svc is None:
        return []
    try:
        return await svc.list_persisted_tasks(limit=_TASK_FETCH_LIMIT, blueprint=blueprint, repo=repo)
    except Exception:  # noqa: BLE001
        logger.debug("analytics: list_persisted_tasks failed", exc_info=True)
        return []


async def _db(request: Request):
    """Lazily connect + cache a Database for agent/LLM/SRE rollups.

    Returns None when the DB is unreachable; callers degrade to empty.
    """
    cached = getattr(request.app.state, "analytics_db", None)
    if cached is not None:
        return cached
    config = getattr(request.app.state, "config", None)
    url = getattr(config, "database_url", "") if config else ""
    if not url:
        return None
    try:
        from devai.services.database import Database

        db = Database(url)
        await db.connect()
        request.app.state.analytics_db = db
        return db
    except Exception:  # noqa: BLE001
        logger.info("analytics: Postgres unavailable — agent/LLM/SRE rollups disabled", exc_info=True)
        request.app.state.analytics_db = None
        return None


# ────────────────────────────────────────────────────────────────────
# Run-level (runtime-sourced)
# ────────────────────────────────────────────────────────────────────


@router.get("/summary")
async def summary(
    request: Request,
    days: int = Query(30, ge=1, le=365),
    blueprint: str | None = None,
    repo: str | None = None,
) -> dict[str, Any]:
    """KPIs + per-state / per-blueprint breakdown for the active runtime."""
    tasks = await _tasks(request, blueprint=blueprint, repo=repo)
    return summarize_runs(tasks, window_days=days)


@router.get("/runs/timeseries")
async def runs_ts(request: Request, days: int = Query(30, ge=1, le=365)) -> list[dict[str, Any]]:
    """Runs/day split by terminal status."""
    tasks = await _tasks(request)
    return runs_timeseries(tasks, days=days)


@router.get("/stages")
async def stages(request: Request) -> list[dict[str, Any]]:
    """Per-stage run count, avg duration, and failures."""
    tasks = await _tasks(request)
    return stage_stats(tasks)


# ────────────────────────────────────────────────────────────────────
# Agent / LLM (Postgres-sourced, best-effort)
# ────────────────────────────────────────────────────────────────────


@router.get("/agents")
async def agents(request: Request, days: int = Query(30, ge=1, le=365)) -> list[dict[str, Any]]:
    """Per-agent executions, avg duration, tokens, cost, failures."""
    db = await _db(request)
    if db is None:
        return []
    try:
        return await db.analytics_agent_stats(days)
    except Exception:  # noqa: BLE001
        logger.debug("analytics: agent stats query failed", exc_info=True)
        return []


@router.get("/llm/cost")
async def llm_cost(request: Request, days: int = Query(30, ge=1, le=365)) -> dict[str, Any]:
    """Token + USD cost by provider/model and over time."""
    db = await _db(request)
    if db is None:
        return {"by_model": [], "timeseries": []}
    by_model: list[dict[str, Any]] = []
    timeseries: list[dict[str, Any]] = []
    try:
        by_model = await db.analytics_llm_cost_by_model(days)
    except Exception:  # noqa: BLE001
        logger.debug("analytics: llm cost-by-model query failed", exc_info=True)
    try:
        timeseries = await db.analytics_llm_cost_timeseries(days)
    except Exception:  # noqa: BLE001
        logger.debug("analytics: llm cost timeseries query failed", exc_info=True)
    return {"by_model": by_model, "timeseries": timeseries}


@router.get("/sre/summary")
async def sre_summary(request: Request) -> dict[str, Any]:
    """Best-effort SRE counts from the shared devai_db SRE tables."""
    db = await _db(request)
    if db is None:
        return {}
    try:
        return await db.analytics_sre_summary()
    except Exception:  # noqa: BLE001
        logger.debug("analytics: SRE summary query failed (tables may be absent)", exc_info=True)
        return {}


# ────────────────────────────────────────────────────────────────────
# Telemetry / OTel collector health
# ────────────────────────────────────────────────────────────────────


@router.get("/telemetry")
async def telemetry(request: Request) -> dict[str, Any]:
    """Telemetry adapter health + Prometheus reachability.

    The page's observability panel reads this: is OTel exporting, to where, and
    is the cluster Prometheus reachable for the live-metrics tie-in.
    """
    config = getattr(request.app.state, "config", None)
    adapter = getattr(request.app.state, "telemetry", None)

    tel: dict[str, Any]
    if adapter is None:
        tel = {"ok": False, "provider": "none", "exporting": False, "endpoint": "", "detail": "no telemetry adapter"}
    else:
        try:
            tel = await adapter.health_check()
        except Exception:  # noqa: BLE001
            tel = {"ok": False, "provider": getattr(adapter, "provider_name", "unknown"), "exporting": False}

    prom_url = getattr(config, "prometheus_url", "") if config else ""
    prom: dict[str, Any] = {"url": prom_url, "reachable": False}
    if prom_url:
        try:
            async with httpx.AsyncClient(timeout=2.5) as client:
                resp = await client.get(f"{prom_url.rstrip('/')}/-/healthy")
                prom["reachable"] = resp.status_code < 500
                prom["status"] = resp.status_code
        except Exception:  # noqa: BLE001
            prom["reachable"] = False

    return {
        "telemetry": tel,
        "prometheus": prom,
        "metrics_enabled": bool(getattr(config, "metrics_enabled", True)) if config else True,
        "provider": getattr(config, "telemetry_provider", "noop") if config else "noop",
    }


__all__ = ["router"]
