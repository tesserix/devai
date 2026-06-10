"""SRE FastAPI server — runs the monitoring pipeline on a schedule + dashboard API.

Runs as a separate deployment from the ALM pipeline. Uses the same PostgreSQL
database for persistence and the same Redis for caching/memory.

Endpoints:
  GET  /healthz              — liveness
  GET  /readyz               — readiness (checks K8s access)
  POST /api/scan/trigger     — manually trigger a scan
  GET  /api/scan/runs        — list recent scan runs
  GET  /api/incidents        — list open incidents
  GET  /api/incidents/{id}   — get incident detail
  PATCH /api/incidents/{id}  — update incident status
  GET  /api/health           — cluster health summary
  GET  /api/apps             — list monitored apps
  GET  /api/metrics          — recent metrics
  GET  /api/costs            — cost reports
"""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

from devai.config import settings
from devai.services.tracing import init_langsmith

logger = logging.getLogger(__name__)

# Auto-scan is disabled by default — manual triggers only.
# Set DEVAI_SRE_AUTO_SCAN=true to re-enable autonomous scanning.
AUTO_SCAN_ENABLED = os.environ.get("DEVAI_SRE_AUTO_SCAN", "false").lower() in ("true", "1", "yes")
DEFAULT_SCAN_INTERVAL = int(os.environ.get("DEVAI_SRE_SCAN_INTERVAL", "300"))
# How often the multi-cadence scheduler wakes to check for due schedules.
SCHEDULE_POLL_INTERVAL = int(os.environ.get("DEVAI_SRE_SCHEDULE_POLL", "60"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown lifecycle."""
    settings.export_langsmith_env()
    init_langsmith()

    # Connect to database. The pool init now retries with backoff
    # (services/database.py); we wrap in a final try so even a total
    # outage doesn't crash the pod — /readyz keeps serving and the
    # SRE scanner can pick up the DB on its next periodic tick.
    from devai.services.database import Database

    db = Database(settings.database_url)
    try:
        await db.connect()
        app.state.db = db
    except Exception:
        logger.exception(
            "SRE database connection failed at startup — running without DB; the scanner will retry per its schedule"
        )
        app.state.db = None

    # Optional: stand up the Fiber-style PipelineService so SRE scans can
    # be driven via the sre-monitor blueprint instead of the hardcoded
    # SREOrchestrator. Falls back gracefully if Redis is unreachable.
    app.state.pipeline_service = None
    if getattr(settings, "pipeline_enabled", False):
        try:
            from devai.core.state import StateManager
            from devai.pipeline.service import PipelineService

            # An SCM client lets the code_remediator open fix PRs + file
            # classified issues. Built only when SCM creds are configured;
            # otherwise the remediator degrades to read-only investigation.
            scm = None
            try:
                from devai.scm import create_scm_client

                if getattr(settings, "scm_token", "") or getattr(settings, "github_app_id", 0):
                    scm = create_scm_client(settings)
                    logger.info("SRE: SCM client wired — code_remediator can open PRs/issues")
            except Exception as e:  # noqa: BLE001
                logger.warning("SRE: SCM client unavailable (%s); code_remediator runs read-only", e)

            state_manager = StateManager(settings.redis_url)
            app.state.pipeline_state_manager = state_manager
            pipeline_service = PipelineService(
                settings,
                scm=scm,
                state_manager=state_manager,
                telemetry=getattr(app.state, "telemetry", None),
            )
            await pipeline_service.start()
            app.state.pipeline_service = pipeline_service
            # Pull any DevAI-authored blueprints published to the shared
            # registry and register them so they're runnable + schedulable.
            await _consume_published_blueprints(pipeline_service)
            logger.info("SRE PipelineService started — blueprint-driven scans enabled")
        except Exception:
            logger.exception("SRE PipelineService failed to start — falling back to legacy SREOrchestrator")

    # Legacy single-blueprint autonomous loop (opt-in via DEVAI_SRE_AUTO_SCAN).
    scan_task = None
    if AUTO_SCAN_ENABLED:
        scan_task = asyncio.create_task(_autonomous_scanner(db, app.state.pipeline_service))
        app.state.scan_task = scan_task
        logger.info("SRE server started — autonomous scanning every %ds", DEFAULT_SCAN_INTERVAL)
    else:
        logger.info("SRE server started — legacy auto-scan DISABLED")

    # Multi-cadence scheduler — runs published SRE blueprints on their own
    # schedules (sre_schedules table). Always on when a DB + pipeline exist;
    # does nothing until a schedule is created via the API.
    sched_task = None
    if app.state.db is not None and app.state.pipeline_service is not None:
        sched_task = asyncio.create_task(_schedule_loop(app.state.db, app.state.pipeline_service))
        app.state.sched_task = sched_task
        logger.info("SRE scheduler started — polling sre_schedules every %ds", SCHEDULE_POLL_INTERVAL)

    yield

    # Shutdown
    if scan_task is not None:
        scan_task.cancel()
    if sched_task is not None:
        sched_task.cancel()
    if app.state.pipeline_service is not None:
        with contextlib_suppress(Exception):
            await app.state.pipeline_service.stop()
        if hasattr(app.state, "pipeline_state_manager"):
            with contextlib_suppress(Exception):
                await app.state.pipeline_state_manager.close()
    telemetry = getattr(app.state, "telemetry", None)
    if telemetry is not None:
        with contextlib_suppress(Exception):
            await telemetry.close()  # flush the OTLP exporter
    await db.close()


def contextlib_suppress(*excs):
    """Tiny shim — saves us a top-level import_alias dance."""
    import contextlib as _c

    return _c.suppress(*excs)


def create_sre_app() -> FastAPI:
    app = FastAPI(
        title="DevAI SRE",
        version="0.1.0",
        description="AI-powered Site Reliability Engineering",
        lifespan=lifespan,
    )

    # Origins are an explicit allowlist (never "*") because allow_credentials
    # is on. Methods/headers are also explicit rather than "*" — the SRE API
    # only serves JSON reads + a few control POST/PATCH calls, so there's no
    # reason to reflect arbitrary methods/headers back with credentials.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3200", "https://sre.tesserix.app"],
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization", "X-Requested-With"],
        allow_credentials=True,
    )

    # Expose settings so the opt-in auth gate (DEVAI_REQUIRE_AUTH) can read it.
    app.state.config = settings

    # Telemetry adapter (adapters/telemetry) — request spans/metrics for the
    # SRE API. Built synchronously so instrument_asgi can register middleware
    # before serving. Noop unless DEVAI_TELEMETRY_PROVIDER=otel; never raises.
    # In-process log ring (live Logs view via the analytics routes).
    try:
        from devai.services.log_buffer import install as install_log_buffer

        install_log_buffer(int(getattr(settings, "log_buffer_capacity", 2000)))
    except Exception:  # noqa: BLE001
        logger.exception("log ring install failed — live Logs view disabled")

    try:
        from devai.adapters.telemetry import create_telemetry_adapter, set_global_telemetry

        app.state.telemetry = create_telemetry_adapter(settings)
        app.state.telemetry.instrument_asgi(app)
        # Process-global sink — the instrumented LLM delegate (SRE agents'
        # calls included) emits into the same exporter.
        set_global_telemetry(app.state.telemetry)
    except Exception:  # noqa: BLE001
        logger.exception("SRE telemetry adapter construction failed — continuing without it")
        app.state.telemetry = None

    @app.middleware("http")
    async def _auth_gate(request, call_next):
        from devai.authz import enforce_auth

        blocked = await enforce_auth(request)
        if blocked is not None:
            return blocked
        return await call_next(request)

    # --- Chat ---

    @app.post("/api/chat/message")
    async def sre_chat_message(request: Request) -> dict[str, str]:
        """Send a message to the SRE chat agent."""
        body = await request.json()
        message = body.get("message", "")
        session_id = body.get("session_id", "default")

        if not message:
            return {"response": "Please send a message.", "session_id": session_id}

        from devai.sre.chat import SREChatAgent

        if not hasattr(app.state, "chat_agent"):
            app.state.chat_agent = SREChatAgent(settings, database=app.state.db)
        agent = app.state.chat_agent
        try:
            response = await agent.chat(message, session_id)
        except Exception as exc:
            logger.exception("SRE chat failed for session %s", session_id)
            from devai.services.redact import redact_secrets

            response = f"Sorry, I encountered an error: {redact_secrets(str(exc))[:200]}"
        return {"response": response, "session_id": session_id}

    # --- Health ---

    @app.get("/healthz")
    async def health():
        return {"status": "ok", "service": "devai-sre"}

    @app.get("/readyz")
    async def ready():
        from devai.sre.tools.k8s_tools import K8sToolExecutor

        k8s = K8sToolExecutor()
        cluster = await k8s._kubectl("cluster-info")
        return {
            "status": "ready" if cluster else "degraded",
            "k8s_access": bool(cluster),
        }

    # --- Scan API ---

    # Track running scans so they don't get garbage collected
    if not hasattr(app.state, "scan_tasks"):
        app.state.scan_tasks = set()

    @app.post("/api/scan/trigger")
    async def trigger_scan(request: Request):
        """Manually trigger an SRE scan."""
        content_type = request.headers.get("content-type", "")
        body = await request.json() if content_type.startswith("application/json") else {}
        cluster_id = body.get("cluster_id", "default")

        # Ensure the cluster exists in sre_clusters (foreign key requirement)
        try:
            await app.state.db.pool.execute(
                """INSERT INTO sre_clusters (id, name, provider, region)
                   VALUES ($1, $2, 'gke', $3)
                   ON CONFLICT (id) DO NOTHING""",
                cluster_id,
                cluster_id,
                "asia-south1",
            )
        except Exception as e:
            logger.error("Failed to ensure cluster row: %s", e)

        # Insert a "running" scan record IMMEDIATELY so the dashboard shows it
        from ulid import ULID

        scan_id = str(ULID())
        try:
            await app.state.db.pool.execute(
                """INSERT INTO sre_scan_runs (id, cluster_id, trigger, status, started_at)
                   VALUES ($1, $2, $3, 'running', NOW())""",
                scan_id,
                cluster_id,
                "manual",
            )
        except Exception as e:
            logger.error("Failed to record scan start: %s", e)
            return {"status": "error", "error": str(e)}

        # Create task and KEEP a reference so it isn't garbage collected
        task = asyncio.create_task(
            _run_single_scan(app.state.db, cluster_id, "manual", scan_id, app.state.pipeline_service)
        )
        app.state.scan_tasks.add(task)
        task.add_done_callback(app.state.scan_tasks.discard)

        return {"status": "triggered", "cluster": cluster_id, "scan_id": scan_id}

    @app.get("/api/scan/runs")
    async def list_scan_runs(limit: int = 20):
        db = app.state.db
        rows = await db.pool.fetch(
            "SELECT * FROM sre_scan_runs ORDER BY started_at DESC LIMIT $1",
            limit,
        )
        return [dict(r) for r in rows]

    # --- Incidents ---

    @app.get("/api/incidents")
    async def list_incidents(status: str = "open", limit: int = 50):
        db = app.state.db
        rows = await db.pool.fetch(
            "SELECT * FROM sre_incidents WHERE status = $1 ORDER BY created_at DESC LIMIT $2",
            status,
            limit,
        )
        return [dict(r) for r in rows]

    @app.get("/api/incidents/{incident_id}")
    async def get_incident(incident_id: str):
        db = app.state.db
        row = await db.pool.fetchrow("SELECT * FROM sre_incidents WHERE id = $1", incident_id)
        if not row:
            raise HTTPException(404, "Incident not found")
        # Get remediations
        remediations = await db.pool.fetch(
            "SELECT * FROM sre_remediations WHERE incident_id = $1 ORDER BY created_at",
            incident_id,
        )
        result = dict(row)
        result["remediations"] = [dict(r) for r in remediations]
        return result

    @app.patch("/api/incidents/{incident_id}")
    async def update_incident(incident_id: str, body: dict[str, Any]):
        db = app.state.db
        status = body.get("status")
        note = body.get("resolution_note", "")
        if status:
            await db.pool.execute(
                "UPDATE sre_incidents SET status = $1, resolution_note = $2, updated_at = NOW() WHERE id = $3",
                status,
                note,
                incident_id,
            )
            if status == "resolved":
                await db.pool.execute(
                    "UPDATE sre_incidents SET resolved_at = NOW(), mttr_seconds = EXTRACT(EPOCH FROM (NOW() - created_at)) WHERE id = $1",
                    incident_id,
                )
        return {"status": "updated"}

    # --- Cluster Health ---

    @app.get("/api/health")
    async def cluster_health():
        db = app.state.db
        rows = await db.pool.fetch("SELECT * FROM v_sre_cluster_health")
        return [dict(r) for r in rows]

    # --- Apps ---

    @app.get("/api/apps")
    async def list_apps():
        db = app.state.db
        rows = await db.pool.fetch("SELECT * FROM v_sre_app_reliability")
        return [dict(r) for r in rows]

    # --- Metrics ---

    @app.get("/api/metrics")
    async def recent_metrics(app_id: str = "", metric_name: str = "", limit: int = 100):
        db = app.state.db
        if app_id:
            rows = await db.pool.fetch(
                "SELECT * FROM sre_metrics WHERE app_id = $1 ORDER BY recorded_at DESC LIMIT $2",
                app_id,
                limit,
            )
        elif metric_name:
            rows = await db.pool.fetch(
                "SELECT * FROM sre_metrics WHERE metric_name = $1 ORDER BY recorded_at DESC LIMIT $2",
                metric_name,
                limit,
            )
        else:
            rows = await db.pool.fetch(
                "SELECT * FROM sre_metrics ORDER BY recorded_at DESC LIMIT $1",
                limit,
            )
        return [dict(r) for r in rows]

    # --- Costs ---

    @app.get("/api/costs")
    async def cost_reports(days: int = 30):
        db = app.state.db
        rows = await db.pool.fetch(
            "SELECT * FROM sre_cost_reports WHERE report_date > CURRENT_DATE - $1 ORDER BY report_date DESC",
            days,
        )

        def _parse_jsonb(value: Any) -> Any:
            if value is None or isinstance(value, dict | list):
                return value
            try:
                return json.loads(value)
            except (TypeError, json.JSONDecodeError):
                return value

        out: list[dict[str, Any]] = []
        for r in rows:
            row = dict(r)
            row["breakdown"] = _parse_jsonb(row.get("breakdown"))
            row["recommendations"] = _parse_jsonb(row.get("recommendations"))
            # Cast Decimal/Date to JSON-serializable types
            if row.get("total_cost_usd") is not None:
                row["total_cost_usd"] = float(row["total_cost_usd"])
            if row.get("monthly_forecast") is not None:
                row["monthly_forecast"] = float(row["monthly_forecast"])
            if row.get("report_date") is not None:
                row["report_date"] = str(row["report_date"])
            out.append(row)
        return out

    # --- Blueprints (what the SRE runtime can run) ---

    @app.get("/api/blueprints")
    async def list_blueprints():
        """SRE blueprints available to run (on-disk + published customs)."""
        ps = app.state.pipeline_service
        if ps is None:
            return []
        out = []
        for bp in ps.list_blueprints():
            meta = bp.get("metadata", {}) or {}
            name = bp.get("name", "")
            # SRE-domain blueprints only.
            if meta.get("domain") == "sre" or name.startswith("sre-"):
                out.append(
                    {
                        "name": name,
                        "description": bp.get("description", ""),
                        "stage_count": bp.get("stage_count", 0),
                        "kind": meta.get("kind", ""),
                        "pattern": meta.get("pattern", ""),
                        "cadence": meta.get("cadence", ""),
                        "title": meta.get("title", name),
                    }
                )
        return out

    @app.get("/api/blueprints/{name}/graph")
    async def blueprint_graph(name: str):
        ps = app.state.pipeline_service
        if ps is None:
            raise HTTPException(status_code=503, detail="pipeline runtime unavailable")
        graph = ps.get_blueprint_graph(name)
        if graph is None:
            raise HTTPException(status_code=404, detail=f"blueprint {name!r} not found")
        return graph

    @app.post("/api/scan/trigger-blueprint")
    async def trigger_blueprint(request: Request):
        """Run a specific (published or built-in) SRE blueprint once."""
        body = await request.json()
        blueprint = body.get("blueprint")
        cluster_id = body.get("cluster_id", "default")
        if not blueprint:
            raise HTTPException(status_code=422, detail="'blueprint' is required")
        from ulid import ULID

        scan_id = str(ULID())
        with contextlib_suppress(Exception):
            await app.state.db.pool.execute(
                "INSERT INTO sre_clusters (id, name, provider, region) VALUES ($1,$2,'gke',$3) "
                "ON CONFLICT (id) DO NOTHING",
                cluster_id,
                cluster_id,
                "asia-south1",
            )
            await app.state.db.pool.execute(
                "INSERT INTO sre_scan_runs (id, cluster_id, trigger, status, started_at) VALUES ($1,$2,'manual','running',NOW())",
                scan_id,
                cluster_id,
            )
        task = asyncio.create_task(
            _run_single_scan(app.state.db, cluster_id, "manual", scan_id, app.state.pipeline_service, blueprint)
        )
        app.state.scan_tasks.add(task)
        task.add_done_callback(app.state.scan_tasks.discard)
        return {"status": "triggered", "blueprint": blueprint, "cluster": cluster_id, "scan_id": scan_id}

    @app.get("/api/scan/runs/{scan_id}/flow")
    async def scan_flow(scan_id: str):
        """Per-stage timing for one scan run, for the dashboard flow view."""
        db = app.state.db
        row = await db.pool.fetchrow("SELECT * FROM sre_scan_runs WHERE id = $1", scan_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"scan {scan_id!r} not found")
        rec = dict(row)
        timings = rec.get("agent_timings")
        if isinstance(timings, str):
            with contextlib_suppress(Exception):
                timings = json.loads(timings)
        blueprint = (timings or {}).get("_blueprint") if isinstance(timings, dict) else None
        graph = None
        if blueprint and app.state.pipeline_service is not None:
            graph = app.state.pipeline_service.get_blueprint_graph(blueprint)
        return {
            "scan_id": scan_id,
            "status": rec.get("status"),
            "blueprint": blueprint,
            "trigger": rec.get("trigger"),
            "incidents_found": rec.get("incidents_found"),
            "agent_timings": timings if isinstance(timings, dict) else {},
            "graph": graph,
            "started_at": str(rec.get("started_at")) if rec.get("started_at") else None,
            "completed_at": str(rec.get("completed_at")) if rec.get("completed_at") else None,
        }

    # --- Observability sources (read-only health; configured in DevAI) ---

    @app.get("/api/observability/sources")
    async def observability_sources():
        """Which observability backends are connected + healthy. Read-only —
        configuration lives in DevAI Settings → Observability."""
        try:
            from devai.adapters.observability import MultiObservabilityAdapter, providers_from_env

            multi = MultiObservabilityAdapter.from_env()
            health = await multi.health()
            return {
                "connected": providers_from_env(),
                "sources": [{"provider": h.provider, "ok": h.ok, "detail": h.detail} for h in health],
            }
        except Exception as e:  # noqa: BLE001
            return {"connected": [], "sources": [], "error": str(e)[:200]}

    # --- Schedules (cadence for published blueprints) ---

    @app.get("/api/schedules")
    async def list_schedules():
        rows = await app.state.db.list_schedules()
        out = []
        for r in rows:
            rec = dict(r)
            for k in ("created_at", "updated_at", "last_run_at"):
                if rec.get(k) is not None and hasattr(rec[k], "isoformat"):
                    rec[k] = rec[k].isoformat()
            out.append(rec)
        return out

    @app.post("/api/schedules", status_code=201)
    async def create_schedule(request: Request):
        body = await request.json()
        blueprint = body.get("blueprint")
        cron = body.get("cron") or body.get("cadence") or ""
        if not blueprint or not cron:
            raise HTTPException(status_code=422, detail="'blueprint' and 'cron' (cadence) are required")
        from ulid import ULID

        sid = str(ULID())
        await app.state.db.create_schedule(
            sid,
            blueprint,
            cron,
            body.get("cluster_id", "default"),
            body.get("created_by", "operator"),
            enabled=bool(body.get("enabled", True)),
        )
        return {"id": sid, "blueprint": blueprint, "cron": cron, "interval_seconds": _cadence_to_seconds(cron)}

    @app.patch("/api/schedules/{schedule_id}")
    async def update_schedule(schedule_id: str, request: Request):
        body = await request.json()
        await app.state.db.update_schedule(schedule_id, cron=body.get("cron"), enabled=body.get("enabled"))
        return {"updated": schedule_id}

    @app.delete("/api/schedules/{schedule_id}")
    async def delete_schedule(schedule_id: str):
        if not await app.state.db.delete_schedule(schedule_id):
            raise HTTPException(status_code=404, detail=f"schedule {schedule_id!r} not found")
        return {"deleted": schedule_id}

    return app


# --- Autonomous Scanner ---


async def _consume_published_blueprints(pipeline_service: Any) -> int:
    """Pull DevAI-authored blueprints from the shared registry and register
    them so the SRE runtime can run + schedule them.

    Best-effort: the on-disk SRE blueprints are always available regardless;
    this only adds the customs published from SRE Studio. The canonical YAML
    round-trips via ``spec.devaiBlueprintYaml`` (see registry mapping).
    """
    registry_url = getattr(settings, "registry_url", "") or ""
    if not registry_url:
        return 0
    try:
        from devai.blueprint.loader import load_blueprint_from_string
        from devai.registry import create_registry_client

        client = create_registry_client(settings)
        if client is None:
            return 0
        envelopes = client._get_collection("/v0/blueprints", "blueprints")  # noqa: SLF001
    except Exception:  # noqa: BLE001
        logger.warning("SRE: could not list published blueprints from registry", exc_info=True)
        return 0

    count = 0
    for env in envelopes:
        try:
            spec = env.get("spec", {}) if isinstance(env, dict) else {}
            yaml_text = spec.get("devaiBlueprintYaml") or ""
            if not yaml_text:
                continue
            bp = load_blueprint_from_string(yaml_text, source="<registry>")
            if pipeline_service.register_blueprint(bp):
                count += 1
        except Exception:  # noqa: BLE001
            logger.debug("SRE: skipped a published blueprint that wouldn't load", exc_info=True)
    if count:
        logger.info("SRE: registered %d published blueprint(s) from the registry", count)
    return count


def _cadence_to_seconds(cadence: str) -> int | None:
    """Parse a schedule cadence into seconds. None = never auto-run.

    Accepts duration shorthand ("30s", "5m", "2h", "1d") and a few words
    ("hourly", "daily", "weekly"). "on-alert" / "manual" / "" → None.
    """
    import re

    c = (cadence or "").strip().lower()
    if not c or c in ("on-alert", "on_alert", "manual", "none"):
        return None
    words = {"hourly": 3600, "daily": 86400, "weekly": 604800}
    if c in words:
        return words[c]
    m = re.match(r"^every\s+(\d+)\s*(second|minute|hour|day)s?$", c)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        return n * {"second": 1, "minute": 60, "hour": 3600, "day": 86400}[unit]
    m = re.match(r"^(\d+)\s*(s|m|h|d)$", c)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        return n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    return None


def _schedule_due(schedule: dict[str, Any]) -> bool:
    """Has enough time elapsed since this schedule last ran?"""
    interval = _cadence_to_seconds(schedule.get("cron", ""))
    if interval is None:
        return False
    last = schedule.get("last_run_at")
    if last is None:
        return True
    import time as _t
    from datetime import datetime

    if isinstance(last, str):
        try:
            last_dt = datetime.fromisoformat(last)
        except ValueError:
            return True
        last_ts = last_dt.timestamp()
    elif hasattr(last, "timestamp"):
        last_ts = last.timestamp()
    else:
        return True
    return (_t.time() - last_ts) >= interval


async def _schedule_loop(db: Any, pipeline_service: Any) -> None:
    """Run published SRE blueprints on their own cadences.

    Wakes every SCHEDULE_POLL_INTERVAL seconds, loads enabled schedules,
    and runs any that are due. Each schedule names a blueprint + cluster.
    """
    while True:
        try:
            schedules = await db.list_schedules(enabled_only=True)
            for sch in schedules:
                if not _schedule_due(sch):
                    continue
                logger.info("SRE scheduler: running blueprint=%s (schedule=%s)", sch.get("blueprint"), sch.get("id"))
                try:
                    await _run_single_scan(
                        db,
                        sch.get("cluster_id", "default"),
                        "cron",
                        None,
                        pipeline_service,
                        blueprint=sch.get("blueprint"),
                    )
                    await db.mark_schedule_ran(sch["id"])
                except Exception:
                    logger.exception("SRE scheduler: blueprint %s failed", sch.get("blueprint"))
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("SRE scheduler poll failed")
        await asyncio.sleep(SCHEDULE_POLL_INTERVAL)


async def _autonomous_scanner(db: Any, pipeline_service: Any = None) -> None:
    """Continuously run SRE scans on a schedule."""
    while True:
        try:
            await _run_single_scan(db, "default", "cron", None, pipeline_service)
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Autonomous SRE scan failed")

        await asyncio.sleep(DEFAULT_SCAN_INTERVAL)


async def _run_single_scan(
    db: Any,
    cluster_id: str,
    trigger: str,
    scan_id: str | None = None,
    pipeline_service: Any = None,
    blueprint: str | None = None,
) -> None:
    """Execute a single SRE scan with the named blueprint.

    Routes through the Fiber-style blueprint when a PipelineService is
    available; otherwise falls back to the legacy `SREOrchestrator`. The
    DB-side `sre_scan_runs` row is updated in both paths. ``blueprint``
    defaults to the configured ``pipeline_sre_blueprint`` (sre-monitor);
    schedules and the trigger-blueprint endpoint pass any published SRE
    blueprint here.
    """
    logger.info(
        "Starting SRE scan: cluster=%s trigger=%s scan_id=%s blueprint=%s", cluster_id, trigger, scan_id, blueprint
    )

    # ── New path: blueprint via PipelineService ──────────────────────
    if pipeline_service is not None:
        try:
            blueprint = blueprint or getattr(settings, "pipeline_sre_blueprint", "sre-monitor")
            task = await pipeline_service.run_once(
                intent=f"SRE scan cluster={cluster_id} trigger={trigger}",
                blueprint=blueprint,
                trigger_type=trigger,
                agent_context={"cluster_id": cluster_id, "trigger": trigger, "scan_id": scan_id},
            )
            findings = task.agent_context.get("correlated_findings", []) or []
            apps = (
                task.agent_context.get("discovery_output", {}).get("apps", [])
                if isinstance(task.agent_context.get("discovery_output"), dict)
                else []
            )
            response = task.agent_context.get("incident_responder_output") or {}
            incidents_created = response.get("incidents_created", 0) if isinstance(response, dict) else 0
            agent_timings = {
                ev.stage: ev.duration_ms / 1000.0 for ev in task.stage_events if ev.phase.value == "completed"
            }
            actual_scan_id = scan_id or task.id

            await _record_scan_row(
                db,
                actual_scan_id,
                cluster_id,
                trigger,
                incidents_created=incidents_created,
                apps_checked=len(apps) if apps else 0,
                findings=len(findings),
                agent_timings=agent_timings,
                pre_created=scan_id is not None,
                blueprint=blueprint,
                task_id=task.id,
            )
            logger.info(
                "SRE blueprint scan complete: scan_id=%s blueprint=%s findings=%d incidents=%d",
                actual_scan_id,
                blueprint,
                len(findings),
                incidents_created,
            )
            return
        except Exception:
            logger.exception("sre-monitor blueprint failed — falling back to legacy SREOrchestrator")

    # ── Legacy path: SREOrchestrator ─────────────────────────────────
    from devai.sre.graph.orchestrator import SREOrchestrator

    try:
        orchestrator = SREOrchestrator(settings, database=db)
        final_state = await orchestrator.run(cluster_id=cluster_id, trigger=trigger)

        # Use the provided scan_id (from trigger endpoint) or the orchestrator's
        actual_scan_id = scan_id or final_state.get("scan_id", "")

        # Update the existing record (or insert if not pre-created)
        try:
            if scan_id:
                await db.pool.execute(
                    """UPDATE sre_scan_runs
                       SET status = 'completed',
                           incidents_found = $2,
                           apps_checked = $3,
                           checks_passed = $4,
                           checks_failed = $5,
                           agent_timings = $6,
                           completed_at = NOW()
                       WHERE id = $1""",
                    actual_scan_id,
                    final_state.get("incidents_created", 0),
                    len(final_state.get("apps", [])),
                    len(final_state.get("all_findings", [])),
                    0,
                    json.dumps(final_state.get("agent_timings", {})),
                )
            else:
                await db.pool.execute(
                    """INSERT INTO sre_scan_runs (id, cluster_id, trigger, status, incidents_found,
                       apps_checked, checks_passed, checks_failed, agent_timings, completed_at)
                       VALUES ($1, $2, $3, 'completed', $4, $5, $6, $7, $8, NOW())""",
                    actual_scan_id,
                    cluster_id,
                    trigger,
                    final_state.get("incidents_created", 0),
                    len(final_state.get("apps", [])),
                    len(final_state.get("all_findings", [])),
                    0,
                    json.dumps(final_state.get("agent_timings", {})),
                )
            logger.info(
                "SRE scan recorded: scan_id=%s apps=%d findings=%d incidents=%d",
                actual_scan_id,
                len(final_state.get("apps", [])),
                len(final_state.get("all_findings", [])),
                final_state.get("incidents_created", 0),
            )
        except Exception as e:
            logger.error("Failed to record scan run: %s", e)
    except Exception as e:
        logger.exception("SRE scan failed: %s", e)
        if scan_id:
            import contextlib

            with contextlib.suppress(Exception):
                await db.pool.execute(
                    """UPDATE sre_scan_runs SET status = 'failed', completed_at = NOW() WHERE id = $1""",
                    scan_id,
                )


async def _record_scan_row(
    db: Any,
    scan_id: str,
    cluster_id: str,
    trigger: str,
    *,
    incidents_created: int,
    apps_checked: int,
    findings: int,
    agent_timings: dict[str, float],
    pre_created: bool,
    blueprint: str = "",
    task_id: str = "",
) -> None:
    """Persist a completed scan into `sre_scan_runs`.

    Used by the blueprint-driven scan path so the dashboard row schema
    stays identical between legacy and new paths. When `pre_created` is
    True the row was inserted by the trigger endpoint; we UPDATE it
    in place. Otherwise we INSERT.
    """
    try:
        if pre_created:
            await db.pool.execute(
                """UPDATE sre_scan_runs
                       SET status = 'completed',
                           incidents_found = $2,
                           apps_checked = $3,
                           checks_passed = $4,
                           checks_failed = $5,
                           agent_timings = $6,
                           completed_at = NOW()
                       WHERE id = $1""",
                scan_id,
                incidents_created,
                apps_checked,
                findings,
                0,
                json.dumps({**agent_timings, "_blueprint": blueprint, "_task_id": task_id}),
            )
        else:
            await db.pool.execute(
                """INSERT INTO sre_scan_runs (id, cluster_id, trigger, status, incidents_found,
                       apps_checked, checks_passed, checks_failed, agent_timings, completed_at)
                       VALUES ($1, $2, $3, 'completed', $4, $5, $6, $7, $8, NOW())""",
                scan_id,
                cluster_id,
                trigger,
                incidents_created,
                apps_checked,
                findings,
                0,
                json.dumps({**agent_timings, "_blueprint": blueprint, "_task_id": task_id}),
            )
    except Exception:
        logger.exception("Failed to record blueprint scan row scan_id=%s", scan_id)


# CLI entrypoint
if __name__ == "__main__":
    import uvicorn

    app = create_sre_app()
    uvicorn.run(app, host="0.0.0.0", port=8090, log_level="info")
