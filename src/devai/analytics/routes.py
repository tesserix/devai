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


# ────────────────────────────────────────────────────────────────────
# Live logs (in-process ring buffer)
# ────────────────────────────────────────────────────────────────────


@router.get("/logs")
async def logs(
    request: Request,  # noqa: ARG001 — uniform route signature
    limit: int = Query(200, ge=1, le=1000),
    level: str = Query("INFO", pattern="^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$"),
    q: str = "",
    logger_prefix: str = "",
) -> dict[str, Any]:
    """Tail of this service's in-process log ring (newest last).

    Live view only — durable history is the GCS archive (see /logs/archive).
    """
    from devai.services.log_buffer import get_buffer

    buf = get_buffer()
    if buf is None:
        return {"entries": [], "stats": {"buffered": 0}, "enabled": False}
    return {
        "entries": buf.tail(limit=limit, min_level=level, q=q, logger_prefix=logger_prefix),
        "stats": buf.stats(),
        "enabled": True,
    }


# ────────────────────────────────────────────────────────────────────
# SLOs (Prometheus burn + SRE incident inputs vs configured targets)
# ────────────────────────────────────────────────────────────────────


async def _prom_query(config: Any, promql: str) -> float | None:
    """Instant PromQL query → scalar; None when unreachable/empty."""
    base = (getattr(config, "prometheus_url", "") or "").rstrip("/")
    if not base:
        return None
    headers = {}
    token = getattr(config, "prometheus_token", "") or ""
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{base}/api/v1/query", params={"query": promql}, headers=headers)
            data = resp.json()
            results = data.get("data", {}).get("result", [])
            if not results:
                return None
            return float(results[0]["value"][1])
    except Exception:  # noqa: BLE001
        return None


@router.get("/slo")
async def slo(request: Request, days: int = Query(7, ge=1, le=90)) -> dict[str, Any]:
    """SLO scorecard: availability + latency + error-rate burn from Prometheus,
    incident MTTR/MTTD + per-app reliability from the SRE tables, all scored
    against the configured targets. Sections degrade to null independently."""
    config = getattr(request.app.state, "config", None)
    window = f"{days}d"

    total = await _prom_query(config, f"sum(increase(devai_http_server_requests_total[{window}]))")
    errors_5xx = await _prom_query(
        config, f'sum(increase(devai_http_server_requests_total{{http_status_code=~"5.."}}[{window}]))'
    )
    p95 = await _prom_query(
        config,
        f"histogram_quantile(0.95, sum(rate(devai_http_server_duration_milliseconds_bucket[{window}])) by (le))",
    )

    availability_pct = error_rate_pct = None
    if total is not None and total > 0:
        bad = errors_5xx or 0.0
        availability_pct = round((1 - bad / total) * 100, 4)
        error_rate_pct = round((bad / total) * 100, 4)

    target_avail = float(getattr(config, "slo_availability_target_pct", 99.9))
    target_p95 = float(getattr(config, "slo_latency_p95_ms", 1500.0))
    target_err = float(getattr(config, "slo_error_rate_target_pct", 1.0))

    # Error budget: the fraction of allowed unavailability already burned.
    budget_burn_pct = None
    if availability_pct is not None:
        allowed = 100.0 - target_avail
        used = 100.0 - availability_pct
        budget_burn_pct = round(min(100.0, (used / allowed) * 100), 2) if allowed > 0 else None

    incidents: dict[str, Any] = {}
    apps: list[dict[str, Any]] = []
    db = await _db(request)
    if db is not None:
        try:
            incidents = await db.analytics_incident_slo(days)
        except Exception:  # noqa: BLE001
            logger.debug("slo: incident query failed", exc_info=True)
        try:
            apps = await db.analytics_app_reliability()
        except Exception:  # noqa: BLE001
            logger.debug("slo: app reliability query failed", exc_info=True)

    return {
        "window_days": days,
        "targets": {
            "availability_pct": target_avail,
            "latency_p95_ms": target_p95,
            "error_rate_pct": target_err,
        },
        "http": {
            "requests": total,
            "availability_pct": availability_pct,
            "error_rate_pct": error_rate_pct,
            "latency_p95_ms": round(p95, 1) if p95 is not None else None,
            "availability_met": (availability_pct >= target_avail) if availability_pct is not None else None,
            "latency_met": (p95 <= target_p95) if p95 is not None else None,
            "error_rate_met": (error_rate_pct <= target_err) if error_rate_pct is not None else None,
            "error_budget_burn_pct": budget_burn_pct,
        },
        "incidents": incidents,
        "apps": apps,
    }


# ────────────────────────────────────────────────────────────────────
# Log archive (GCS bucket the devai-log-archiver CronJob writes)
# ────────────────────────────────────────────────────────────────────


@router.get("/logs/archive")
async def logs_archive(request: Request, limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
    """The archive policy + a live object listing when the pod has GCS access.

    Policy values are config (always shown); the listing degrades to [] without
    creds so the panel still renders the bucket + lifecycle + purge settings.
    """
    config = getattr(request.app.state, "config", None)
    bucket = getattr(config, "log_archive_bucket", "") if config else ""
    purge_days = int(getattr(config, "log_purge_days", 15)) if config else 15

    policy = {
        "bucket": bucket,
        "purge_days": purge_days,
        # Mirrors the bucket's lifecycle rules (tesserix-k8s owns the bucket).
        "lifecycle": [
            {"after_days": 2, "action": "SetStorageClass", "value": "NEARLINE"},
            {"after_days": 7, "action": "SetStorageClass", "value": "COLDLINE"},
            {"after_days": purge_days, "action": "Delete", "value": ""},
        ],
        "archiver": "CronJob devai-log-archiver (every 6h) — pod logs → gs://" + (bucket or "<unset>"),
        "purger": f"CronJob devai-log-purge (daily) — deletes objects older than {purge_days}d",
    }

    objects: list[dict[str, Any]] = []
    total_bytes = 0
    if bucket:
        adapter = getattr(request.app.state, "log_archive_store", None)
        if adapter is None:
            try:
                from devai.adapters.object_store.gcs import GCSObjectStoreAdapter

                adapter = GCSObjectStoreAdapter(bucket=bucket)
            except Exception:  # noqa: BLE001 — SDK/creds absent → policy-only panel
                adapter = None
            request.app.state.log_archive_store = adapter
        if adapter is not None:
            objects = await adapter.list_objects(limit=limit)
            total_bytes = sum(o.get("size", 0) for o in objects)

    return {"policy": policy, "objects": objects, "listed_bytes": total_bytes, "listing_available": bool(objects)}


__all__ = ["router"]
