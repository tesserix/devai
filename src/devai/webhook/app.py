"""FastAPI webhook application factory."""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

if TYPE_CHECKING:
    from devai.config import Settings
    from devai.core.event_bus import EventBus
    from devai.core.state import StateManager

_START_TIME = time.time()


def create_app(event_bus: EventBus, state: StateManager, config: Settings) -> FastAPI:
    """Create the FastAPI app with shared resources injected."""
    app = FastAPI(title="DevAI", version="0.2.0", description="AI-powered ALM Pipeline")

    # Store shared resources for access in routes
    app.state.event_bus = event_bus
    app.state.state_manager = state
    app.state.config = config

    # Webhook routes
    from devai.webhook.routes import router as webhook_router

    app.include_router(webhook_router)

    # Dashboard routes (UI + API)
    from devai.dashboard.routes import router as dashboard_router

    app.include_router(dashboard_router)

    # Chat routes (chatbot API + WebSocket)
    from devai.chat.routes import router as chat_router

    app.include_router(chat_router)

    # Serve dashboard static files (CSS, JS)
    static_dir = Path(__file__).parent.parent / "dashboard" / "static"
    if static_dir.exists():
        app.mount("/dashboard/static", StaticFiles(directory=str(static_dir)), name="dashboard-static")

    # --- Health & Readiness ---

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        """Liveness probe — returns 200 if process is alive."""
        return {"status": "ok", "uptime": f"{time.time() - _START_TIME:.0f}s"}

    @app.get("/readyz")
    async def ready() -> JSONResponse:
        """Readiness probe — checks Redis and NATS connectivity."""
        checks: dict[str, str] = {}

        # Redis check
        try:
            await state.redis.ping()
            checks["redis"] = "ok"
        except Exception as e:
            checks["redis"] = f"error: {e}"

        # NATS check
        try:
            if event_bus._nc and not event_bus._nc.is_closed:
                checks["nats"] = "ok"
            else:
                checks["nats"] = "disconnected"
        except Exception as e:
            checks["nats"] = f"error: {e}"

        all_ok = all(v == "ok" for v in checks.values())
        return JSONResponse(
            content={"status": "ready" if all_ok else "not_ready", "checks": checks},
            status_code=200 if all_ok else 503,
        )

    # --- A2A Messages API ---

    @app.get("/dashboard/api/pipeline/runs/{run_id}/a2a")
    async def get_a2a_messages(run_id: str) -> list:
        """Get A2A messages for a pipeline run."""
        import json

        raw = await state.redis.lrange(f"devai:run:{run_id}:a2a_messages", 0, -1)
        return [json.loads(m) for m in raw]

    # --- Memory API ---

    @app.get("/dashboard/api/memory")
    async def list_memories(
        agent: str = "",
        repo: str = "",
        memory_type: str = "",
        limit: int = 20,
    ) -> list:
        """List agent memories."""
        from devai.services.memory import AgentMemory

        memory = AgentMemory(state.redis)
        entries = await memory.recall(
            agent=agent or None,
            repo=repo or None,
            memory_type=memory_type or None,
            limit=limit,
        )
        return [e.to_dict() for e in entries]

    # --- Audit Trail API ---

    @app.get("/dashboard/api/audit/{run_id}")
    async def get_audit_trail(run_id: str, limit: int = 100) -> list:
        """Get the guardrail audit trail for a pipeline run."""
        from devai.services.guardrails import AuditLog

        audit = AuditLog(state.redis)
        return await audit.get_audit_trail(run_id, limit)

    @app.get("/dashboard/api/audit/{run_id}/security")
    async def get_security_events(run_id: str) -> list:
        """Get security-relevant audit events for a pipeline run."""
        from devai.services.guardrails import AuditLog

        audit = AuditLog(state.redis)
        trail = await audit.get_audit_trail(run_id, 500)
        return [e for e in trail if e.get("severity") == "critical" or "security" in e.get("action", "")]

    @app.get("/dashboard/api/ratelimits")
    async def get_rate_limits() -> dict:
        """Get current rate limit status for all resources."""
        from devai.services.guardrails import DEFAULT_RATE_LIMITS, RateLimiter

        limiter = RateLimiter(state.redis)
        statuses = {}
        for resource in DEFAULT_RATE_LIMITS:
            statuses[resource] = await limiter.check(resource)
        return statuses

    return app
