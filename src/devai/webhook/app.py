"""FastAPI webhook application factory."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

if TYPE_CHECKING:
    from devai.config import Settings
    from devai.core.event_bus import EventBus
    from devai.core.state import StateManager


def create_app(event_bus: EventBus, state: StateManager, config: Settings) -> FastAPI:
    """Create the FastAPI app with shared resources injected."""
    app = FastAPI(title="DevAI", version="0.1.0")

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

    # Serve dashboard static files (CSS, JS)
    static_dir = Path(__file__).parent.parent / "dashboard" / "static"
    app.mount("/dashboard/static", StaticFiles(directory=str(static_dir)), name="dashboard-static")

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def ready() -> dict[str, str]:
        return {"status": "ok"}

    return app
