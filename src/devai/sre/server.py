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


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown lifecycle."""
    settings.export_langsmith_env()
    init_langsmith()

    # Connect to database
    from devai.services.database import Database

    db = Database(settings.database_url)
    await db.connect()
    app.state.db = db

    # Optional: stand up the Fiber-style PipelineService so SRE scans can
    # be driven via the sre-monitor blueprint instead of the hardcoded
    # SREOrchestrator. Falls back gracefully if Redis is unreachable.
    app.state.pipeline_service = None
    if getattr(settings, "pipeline_enabled", False):
        try:
            from devai.core.state import StateManager
            from devai.pipeline.service import PipelineService

            state_manager = StateManager(settings.redis_url)
            app.state.pipeline_state_manager = state_manager
            pipeline_service = PipelineService(
                settings,
                scm=None,  # SRE doesn't need an SCM client
                state_manager=state_manager,
            )
            await pipeline_service.start()
            app.state.pipeline_service = pipeline_service
            logger.info("SRE PipelineService started — sre-monitor blueprint is the default scan path")
        except Exception:
            logger.exception("SRE PipelineService failed to start — falling back to legacy SREOrchestrator")

    # Start the autonomous scanner only if explicitly enabled
    scan_task = None
    if AUTO_SCAN_ENABLED:
        scan_task = asyncio.create_task(_autonomous_scanner(db, app.state.pipeline_service))
        app.state.scan_task = scan_task
        logger.info("SRE server started — autonomous scanning every %ds", DEFAULT_SCAN_INTERVAL)
    else:
        logger.info("SRE server started — auto-scan DISABLED, manual triggers only")

    yield

    # Shutdown
    if scan_task is not None:
        scan_task.cancel()
    if app.state.pipeline_service is not None:
        with contextlib_suppress(Exception):
            await app.state.pipeline_service.stop()
        if hasattr(app.state, "pipeline_state_manager"):
            with contextlib_suppress(Exception):
                await app.state.pipeline_state_manager.close()
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

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3200", "https://sre.tesserix.app"],
        allow_methods=["*"],
        allow_headers=["*"],
        allow_credentials=True,
    )

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
            response = f"Sorry, I encountered an error: {exc}"
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
            if value is None or isinstance(value, (dict, list)):
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

    return app


# --- Autonomous Scanner ---


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
) -> None:
    """Execute a single SRE monitoring scan.

    Routes through the Fiber-style `sre-monitor` blueprint when a
    PipelineService is available; otherwise falls back to the legacy
    `SREOrchestrator`. The DB-side `sre_scan_runs` row is updated in
    both paths so the dashboard sees the same rows.
    """
    logger.info("Starting SRE scan: cluster=%s trigger=%s scan_id=%s", cluster_id, trigger, scan_id)

    # ── New path: sre-monitor blueprint via PipelineService ──────────
    if pipeline_service is not None:
        try:
            blueprint = getattr(settings, "pipeline_sre_blueprint", "sre-monitor")
            task = await pipeline_service.run_once(
                intent=f"SRE scan cluster={cluster_id} trigger={trigger}",
                blueprint=blueprint,
                trigger_type=trigger,
                agent_context={"cluster_id": cluster_id, "trigger": trigger, "scan_id": scan_id},
            )
            findings = task.agent_context.get("correlated_findings", []) or []
            apps = task.agent_context.get("discovery_output", {}).get("apps", []) if isinstance(
                task.agent_context.get("discovery_output"), dict
            ) else []
            response = task.agent_context.get("incident_responder_output") or {}
            incidents_created = (
                response.get("incidents_created", 0) if isinstance(response, dict) else 0
            )
            agent_timings = {
                ev.stage: ev.duration_ms / 1000.0
                for ev in task.stage_events
                if ev.phase.value == "completed"
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
