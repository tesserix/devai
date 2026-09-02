"""FastAPI routes for the analytics surface.

Mounted at `/api/analytics/*` by `devai.webhook.app.create_app`. Read-only,
with one governance exception: DELETE /memory/{id} (the dashboard's memory
panel removes wrong/stale learnings through it).

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

from devai.analytics.service import runs_timeseries, stage_stats, summarize_lifecycle_evals, summarize_runs

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
    tenant_id, user_id, _ = await _usage_scope(request)
    db = await _db(request)
    if db is None:
        return []
    try:
        return await db.analytics_agent_stats(days, tenant_id=tenant_id, user_id=user_id)
    except Exception:  # noqa: BLE001
        logger.debug("analytics: agent stats query failed", exc_info=True)
        return []


@router.get("/llm/cost")
async def llm_cost(request: Request, days: int = Query(30, ge=1, le=365)) -> dict[str, Any]:
    """Token + USD cost by provider/model and over time."""
    tenant_id, user_id, _ = await _usage_scope(request)
    db = await _db(request)
    if db is None:
        return {"by_model": [], "timeseries": []}
    by_model: list[dict[str, Any]] = []
    timeseries: list[dict[str, Any]] = []
    try:
        by_model = await db.analytics_llm_cost_by_model(days, tenant_id=tenant_id, user_id=user_id)
    except Exception:  # noqa: BLE001
        logger.debug("analytics: llm cost-by-model query failed", exc_info=True)
    try:
        timeseries = await db.analytics_llm_cost_timeseries(days, tenant_id=tenant_id, user_id=user_id)
    except Exception:  # noqa: BLE001
        logger.debug("analytics: llm cost timeseries query failed", exc_info=True)
    return {"by_model": by_model, "timeseries": timeseries}


# ────────────────────────────────────────────────────────────────────
# LLM usage ledger (Redis) — real cost/tokens/latency per model + per user,
# captured for EVERY run (not just legacy agent_executions writers).
# ────────────────────────────────────────────────────────────────────


def _ledger(request: Request):
    return getattr(request.app.state, "usage_ledger", None)


async def _usage_scope(request: Request) -> tuple[str, str, bool]:
    """Return ``(tenant, subject, can_list_users)`` for the caller.

    Tenant admins see their tenant aggregate. Only an explicit platform-admin
    may read the cross-tenant global aggregate. Anonymous callers are rejected
    before any empty/global namespace can be selected.
    """
    from devai.authz import require_principal

    principal = await require_principal(request)
    roles = set(getattr(principal, "roles", None) or [])
    tenant = principal.tenant_id or ""
    if "platform-admin" in roles:
        return "", "", True
    if "admin" in roles:
        return tenant, "", True
    return tenant, (principal.uid or principal.email or ""), False


@router.get("/usage")
async def usage(request: Request, days: int = Query(30, ge=1, le=365)) -> dict[str, Any]:
    """Cost / tokens / latency the caller is allowed to see: their OWN usage
    (per-model + timeseries), or — for admins — the global view plus the
    per-user breakdown. All with real USD cost."""
    ledger = _ledger(request)
    if ledger is None:
        return {
            "summary": {},
            "by_model": [],
            "by_user": [],
            "by_sandbox": [],
            "timeseries": [],
            "enabled": False,
            "scope": "none",
        }
    scope_tenant, scope_user, can_list_users = await _usage_scope(request)
    return {
        "summary": await ledger.summary(scope_user, scope_tenant),
        "by_model": await ledger.by_model(scope_user, scope_tenant),
        "by_user": await ledger.by_user(scope_tenant) if can_list_users else [],
        "by_sandbox": await ledger.by_sandbox(scope_tenant, "" if can_list_users else scope_user),
        "timeseries": await ledger.timeseries(days, scope_user, scope_tenant),
        "enabled": True,
        "scope": "all" if can_list_users and not scope_tenant else ("tenant" if can_list_users else "me"),
    }


@router.get("/usage/recent")
async def usage_recent(request: Request, limit: int = Query(100, ge=1, le=300)) -> list[dict[str, Any]]:
    """The caller's most recent LLM calls (admins see the global stream)."""
    ledger = _ledger(request)
    if ledger is None:
        return []
    scope_tenant, scope_user, _ = await _usage_scope(request)
    return await ledger.recent(limit, scope_user, scope_tenant)


@router.get("/pricing")
async def pricing(request: Request) -> dict[str, Any]:
    """The rate card the cost figures are computed from (USD per 1M tokens)."""
    from devai.analytics.pricing import rate_card

    return {"unit": "USD per 1,000,000 tokens", "rates": rate_card()}


@router.get("/evals")
async def evals(request: Request, days: int = Query(30, ge=1, le=365)) -> dict[str, Any]:
    """Quality eval scores the caller may see — their OWN runs, or (admin)
    everyone's. Pass-rate, average score, by-evaluator, recent."""
    config = getattr(request.app.state, "config", None)
    detail_url = str(getattr(config, "langfuse_public_url", "") or "")
    db = await _db(request)
    if db is None:
        return {
            "summary": {"evals": 0, "avg_score": 0, "pass_rate": 0},
            "by_evaluator": [],
            "recent": [],
            "lifecycle": {**summarize_lifecycle_evals([]), "detail_url": detail_url},
            "scope": "none",
        }
    scope_tenant, scope_user, can_list_users = await _usage_scope(request)
    out: dict[str, Any] = await db.analytics_evals(
        days,
        tenant_id=scope_tenant,
        user_id="" if can_list_users else scope_user,
    )
    try:
        lifecycle_rows = await db.analytics_lifecycle_eval_runs(
            days,
            tenant_id=scope_tenant,
            user_id="" if can_list_users else scope_user,
        )
    except Exception:  # noqa: BLE001 — older schemas degrade to the empty scorecard
        logger.debug("analytics: durable evaluation rollup unavailable", exc_info=True)
        lifecycle_rows = []
    lifecycle = summarize_lifecycle_evals(lifecycle_rows)
    lifecycle["detail_url"] = detail_url
    out["lifecycle"] = lifecycle
    out["scope"] = "all" if can_list_users and not scope_tenant else ("tenant" if can_list_users else "me")
    return out


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
# Memory (adapter health + corpus stats + recent records)
# ────────────────────────────────────────────────────────────────────


@router.get("/memory")
async def memory(request: Request, days: int = Query(30, ge=1, le=365)) -> dict[str, Any]:
    """Memory analytics: configured provider + health, corpus totals
    (by type / embedding coverage / recency), per-agent + per-repo
    breakdowns, a writes-per-day timeseries, and the latest records.

    Corpus sections come from agent_memories (pgvector store) and degrade
    to empty when Postgres is unreachable; provider/health and the recent
    list always come from the configured MemoryAdapter so the panel is
    honest about what the runtime actually uses.
    """
    from devai.adapters.memory.runtime import get_global_memory

    adapter = get_global_memory()
    out: dict[str, Any] = {
        "provider": adapter.provider_name,
        "health": {"ok": False, "detail": "unavailable"},
        "totals": {},
        "by_agent": [],
        "by_repo": [],
        "timeseries": [],
        "recent": [],
    }
    try:
        out["health"] = await adapter.health_check()
    except Exception:  # noqa: BLE001
        logger.debug("memory analytics: health_check failed", exc_info=True)

    try:
        records = await adapter.recall(limit=15)
        out["recent"] = [
            {
                "provider_id": r.provider_id,
                "agent": r.agent,
                "repo": r.repo,
                "type": getattr(r.memory_type, "value", r.memory_type),
                "content": r.content[:280],
                "tags": r.tags,
                "access_count": r.access_count,
                "created_at": r.created_at,
            }
            for r in records
        ]
    except Exception:  # noqa: BLE001
        logger.debug("memory analytics: recent recall failed", exc_info=True)

    db = await _db(request)
    if db is not None:
        try:
            out["totals"] = await db.analytics_memory_summary()
        except Exception:  # noqa: BLE001
            logger.debug("memory analytics: summary query failed", exc_info=True)
        try:
            out["by_agent"] = await db.analytics_memory_by_agent()
        except Exception:  # noqa: BLE001
            logger.debug("memory analytics: by-agent query failed", exc_info=True)
        try:
            out["by_repo"] = await db.analytics_memory_by_repo()
        except Exception:  # noqa: BLE001
            logger.debug("memory analytics: by-repo query failed", exc_info=True)
        try:
            out["timeseries"] = await db.analytics_memory_timeseries(days)
        except Exception:  # noqa: BLE001
            logger.debug("memory analytics: timeseries query failed", exc_info=True)

    return out


@router.delete("/memory/{provider_id}")
async def memory_forget(provider_id: str) -> dict[str, Any]:
    """Governance: delete one memory record by id (soft delete on pgvector).

    The one mutating endpoint on this router — it exists so the dashboard's
    memory panel can remove a wrong or stale learning before it keeps
    getting injected into future runs.
    """
    from devai.adapters.memory.runtime import get_global_memory

    adapter = get_global_memory()
    try:
        removed = await adapter.forget(provider_id)
    except Exception:  # noqa: BLE001
        logger.exception("memory forget failed for %s", provider_id)
        removed = False
    return {"removed": removed, "provider": adapter.provider_name, "id": provider_id}


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

    # Trace backend (Tempo via the collector) + a browser deep-link into
    # Grafana Explore filtered to the DevAI service. The dashboard shows the
    # link only when grafana_public_url is configured.
    grafana_public = getattr(config, "grafana_public_url", "") if config else ""
    ds_uid = getattr(config, "traces_datasource_uid", "tempo") if config else "tempo"
    service = getattr(config, "otel_service_name", "devai") if config else "devai"
    explore_link = ""
    if grafana_public:
        import json as _json
        from urllib.parse import quote

        # Grafana Explore deep link: Tempo datasource, TraceQL filtered to the
        # service. Grafana parses the `left` panel state from the query string.
        left = _json.dumps(
            {
                "datasource": ds_uid,
                "queries": [
                    {"refId": "A", "queryType": "traceql", "query": f'{{ resource.service.name = "{service}" }}'}
                ],
                "range": {"from": "now-6h", "to": "now"},
            }
        )
        explore_link = f"{grafana_public.rstrip('/')}/explore?left={quote(left)}"

    return {
        "telemetry": tel,
        "prometheus": prom,
        "traces": {
            "backend": getattr(config, "traces_backend", "tempo") if config else "tempo",
            "exporting": bool(tel.get("exporting")),
            "grafana_url": grafana_public,
            "explore_link": explore_link,
            "langsmith_project": getattr(config, "langchain_project", "") if config else "",
        },
        "metrics_enabled": bool(getattr(config, "metrics_enabled", True)) if config else True,
        "provider": getattr(config, "telemetry_provider", "noop") if config else "noop",
    }


# ────────────────────────────────────────────────────────────────────
# Fleet telemetry (per-agent metrics from Prometheus / the OTel collector)
# ────────────────────────────────────────────────────────────────────


async def _prom_query_vector(config: Any, promql: str) -> list[dict[str, Any]]:
    """Instant PromQL query → list of {labels, value}; [] when unreachable."""
    base = (getattr(config, "prometheus_url", "") or "").rstrip("/")
    if not base:
        return []
    headers = {}
    token = getattr(config, "prometheus_token", "") or ""
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{base}/api/v1/query", params={"query": promql}, headers=headers)
            results = resp.json().get("data", {}).get("result", [])
            return [{"labels": r.get("metric", {}), "value": float(r["value"][1])} for r in results]
    except Exception:  # noqa: BLE001
        return []


@router.get("/fleet")
async def fleet(request: Request, days: int = Query(30, ge=1, le=90)) -> dict[str, Any]:
    """Per-agent (fleet) telemetry from Prometheus — the OTel metrics the
    collector renders. Runs, failures, p95 stage latency, LLM tokens and cost
    keyed by agent persona. Degrades to an empty, disabled payload when
    Prometheus is unreachable (the page then shows the Postgres rollups)."""
    config = getattr(request.app.state, "config", None)
    base = (getattr(config, "prometheus_url", "") or "") if config else ""
    if not base:
        return {"enabled": False, "agents": [], "window_days": days}
    window = f"{days}d"

    def _by_agent(rows: list[dict[str, Any]]) -> dict[str, float]:
        out: dict[str, float] = {}
        for r in rows:
            agent = r["labels"].get("agent") or ""
            if agent:
                out[agent] = out.get(agent, 0.0) + r["value"]
        return out

    runs = _by_agent(await _prom_query_vector(config, f"sum by (agent) (increase(devai_agent_runs_total[{window}]))"))
    fails = _by_agent(
        await _prom_query_vector(
            config, f'sum by (agent) (increase(devai_agent_runs_total{{status="failed"}}[{window}]))'
        )
    )
    tokens = _by_agent(await _prom_query_vector(config, f"sum by (agent) (increase(devai_llm_tokens_total[{window}]))"))
    # The OTel→Prometheus exporter appends the instrument unit, so the cost
    # counter (devai.llm.cost_usd, unit USD) lands as devai_llm_cost_usd_USD_total.
    cost = _by_agent(
        await _prom_query_vector(config, f"sum by (agent) (increase(devai_llm_cost_usd_USD_total[{window}]))")
    )
    p95 = _by_agent(
        await _prom_query_vector(
            config,
            "histogram_quantile(0.95, sum by (agent, le) "
            f"(rate(devai_pipeline_stage_duration_milliseconds_bucket[{window}])))",
        )
    )

    names = sorted(set(runs) | set(tokens) | set(cost), key=lambda a: -(runs.get(a, 0) + cost.get(a, 0)))
    agents = [
        {
            "agent": name,
            "runs": round(runs.get(name, 0.0)),
            "failures": round(fails.get(name, 0.0)),
            "p95_latency_ms": round(p95.get(name, 0.0)) if name in p95 else None,
            "tokens": round(tokens.get(name, 0.0)),
            "cost_usd": round(cost.get(name, 0.0), 4),
        }
        for name in names
    ]
    return {"enabled": True, "window_days": days, "agents": agents, "source": "prometheus"}


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
