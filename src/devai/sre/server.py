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

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from devai.config import settings
from devai.services.tracing import init_langsmith

logger = logging.getLogger(__name__)

# Scan interval (seconds)
DEFAULT_SCAN_INTERVAL = 300  # 5 minutes


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

    # Start the autonomous scanner
    scan_task = asyncio.create_task(_autonomous_scanner(db))
    app.state.scan_task = scan_task

    logger.info("SRE server started — autonomous scanning every %ds", DEFAULT_SCAN_INTERVAL)

    yield

    # Shutdown
    scan_task.cancel()
    await db.close()


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

    @app.post("/api/scan/trigger")
    async def trigger_scan(cluster_id: str = "default"):
        """Manually trigger an SRE scan."""
        asyncio.create_task(_run_single_scan(app.state.db, cluster_id, "manual"))
        return {"status": "triggered", "cluster": cluster_id}

    @app.get("/api/scan/runs")
    async def list_scan_runs(limit: int = 20):
        db = app.state.db
        rows = await db.pool.fetch(
            "SELECT * FROM sre_scan_runs ORDER BY started_at DESC LIMIT $1", limit,
        )
        return [dict(r) for r in rows]

    # --- Incidents ---

    @app.get("/api/incidents")
    async def list_incidents(status: str = "open", limit: int = 50):
        db = app.state.db
        rows = await db.pool.fetch(
            "SELECT * FROM sre_incidents WHERE status = $1 ORDER BY created_at DESC LIMIT $2",
            status, limit,
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
            "SELECT * FROM sre_remediations WHERE incident_id = $1 ORDER BY created_at", incident_id,
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
                status, note, incident_id,
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
                app_id, limit,
            )
        elif metric_name:
            rows = await db.pool.fetch(
                "SELECT * FROM sre_metrics WHERE metric_name = $1 ORDER BY recorded_at DESC LIMIT $2",
                metric_name, limit,
            )
        else:
            rows = await db.pool.fetch(
                "SELECT * FROM sre_metrics ORDER BY recorded_at DESC LIMIT $1", limit,
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
        return [dict(r) for r in rows]

    return app


# --- Autonomous Scanner ---

async def _autonomous_scanner(db: Any) -> None:
    """Continuously run SRE scans on a schedule."""
    while True:
        try:
            await _run_single_scan(db, "default", "cron")
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Autonomous SRE scan failed")

        await asyncio.sleep(DEFAULT_SCAN_INTERVAL)


async def _run_single_scan(db: Any, cluster_id: str, trigger: str) -> None:
    """Execute a single SRE monitoring scan."""
    from devai.sre.graph.orchestrator import SREOrchestrator

    orchestrator = SREOrchestrator(settings, database=db)
    final_state = await orchestrator.run(cluster_id=cluster_id, trigger=trigger)

    # Record the scan run
    try:
        await db.pool.execute(
            """INSERT INTO sre_scan_runs (id, cluster_id, trigger, status, incidents_found,
               apps_checked, checks_passed, checks_failed, agent_timings, completed_at)
               VALUES ($1, $2, $3, 'completed', $4, $5, $6, $7, $8, NOW())""",
            final_state.get("scan_id", ""),
            cluster_id,
            trigger,
            final_state.get("incidents_created", 0),
            len(final_state.get("apps", [])),
            len(final_state.get("all_findings", [])),
            0,
            json.dumps(final_state.get("agent_timings", {})),
        )
    except Exception as e:
        logger.error("Failed to record scan run: %s", e)


# CLI entrypoint
if __name__ == "__main__":
    import uvicorn

    app = create_sre_app()
    uvicorn.run(app, host="0.0.0.0", port=8090, log_level="info")
