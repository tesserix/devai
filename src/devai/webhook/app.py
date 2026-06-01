"""FastAPI webhook application factory."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

if TYPE_CHECKING:
    from devai.adapters.event_bus.base import EventBusAdapter
    from devai.config import Settings
    from devai.core.event_bus import EventBus
    from devai.core.state import StateManager

logger = logging.getLogger(__name__)
_START_TIME = time.time()


def create_app(
    event_bus: EventBus,
    state: StateManager,
    config: Settings,
    *,
    event_bus_adapter: EventBusAdapter | None = None,
) -> FastAPI:
    """Create the FastAPI app with shared resources injected.

    `event_bus` is the legacy NATS JetStream wrapper that the
    LangGraph-era PipelineOrchestrator still uses.

    `event_bus_adapter` is the adapter-pattern wrapper that the new
    PipelineService publishes through (and that legacy NATS subscribers
    started via `devai start-agent` consume). Optional — if not passed,
    PipelineService builds its own.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Build the aregistry client FIRST so every downstream service —
        # PipelineService stages, SpecializationService, dashboard
        # routes — shares the same instance (and the same 30 s cache).
        # Previously PipelineService started before the registry client
        # existed, which silently dropped registry-based image / profile
        # lookups in the JobRunnerStage path.
        try:
            from devai.registry import create_registry_client

            _registry_client = create_registry_client(config)
        except Exception:
            logger.exception("registry client construction failed — running in pure-local mode")
            _registry_client = None
        app.state.registry_client = _registry_client

        # Start the Fiber-style pipeline runtime when enabled. SCM is
        # constructed lazily so this doesn't trip start-up when the SCM
        # provider isn't configured yet.
        pipeline_service = None
        if getattr(config, "pipeline_enabled", False):
            try:
                from devai.pipeline.service import PipelineService
                from devai.scm import create_scm_client

                scm = None
                try:
                    scm = create_scm_client(config)
                except Exception as e:  # noqa: BLE001
                    logger.warning("PipelineService: SCM construction failed (%s); stages run with no SCM", e)

                pipeline_service = PipelineService(
                    config,
                    scm=scm,
                    state_manager=state,
                    event_bus=event_bus,
                    event_bus_adapter=event_bus_adapter,
                    registry_client=_registry_client,
                )
                await pipeline_service.start()
                app.state.pipeline_service = pipeline_service
            except Exception:
                logger.exception("PipelineService failed to start — continuing without it")
                app.state.pipeline_service = None
        else:
            app.state.pipeline_service = None
            logger.info("PipelineService disabled (DEVAI_PIPELINE_ENABLED is not true)")

        # SpecializationService — independent of the pipeline runtime so
        # the dashboard can browse the YAML catalog even when the
        # blueprint executor is disabled.
        spec_service = None
        if getattr(config, "specializations_enabled", True):
            try:
                from devai.specializations.service import SpecializationService

                spec_service = SpecializationService(
                    config, registry_client=_registry_client
                )
                await spec_service.start()
                app.state.specialization_service = spec_service
            except Exception:
                logger.exception("SpecializationService failed to start")
                app.state.specialization_service = None
        else:
            app.state.specialization_service = None

        # Authoring service — lets the dashboard create custom agents +
        # blueprints. Redis-backed (durable), registering authored agents
        # into the live specialization registry so they're runnable at
        # once. Degrades to an in-memory store if Redis is unreachable.
        app.state.authoring_service = None
        try:
            from devai.authoring import create_authoring_service

            spec_registry = getattr(spec_service, "registry", None) if spec_service else None
            redis = getattr(state, "redis", None)
            authoring_service = create_authoring_service(redis=redis, spec_registry=spec_registry)
            await authoring_service.load_into_registry()
            app.state.authoring_service = authoring_service
            logger.info("Authoring service ready (store=%s)", "redis" if redis else "in-memory")
        except Exception:
            logger.exception("Authoring service failed to start — authoring API will 503")
            app.state.authoring_service = None

        # Repo onboarding service (Repos page). Independent of the
        # pipeline runtime: build an SCM client (reuse the pipeline's if
        # one exists) + a best-effort Postgres pool, and fall back to the
        # in-memory store when the DB is unreachable. The reconciler can
        # rebuild the cache from the `.platform/devai.yaml` markers, so an
        # in-memory store loses nothing permanent.
        onboarding_db = None
        app.state.onboarding_service = None
        try:
            from devai.onboarding import create_onboarding_service
            from devai.scm import create_scm_client

            onboarding_scm = getattr(app.state, "scm_client", None)
            if onboarding_scm is None:
                onboarding_scm = create_scm_client(config)
                app.state.scm_client = onboarding_scm

            pool = None
            try:
                from devai.services.database import Database

                onboarding_db = Database(config.database_url)
                await onboarding_db.connect()
                pool = onboarding_db.pool
            except Exception as e:  # noqa: BLE001
                logger.warning("Onboarding: DB pool unavailable (%s) — using in-memory store", e)
                onboarding_db = None

            app.state.onboarding_service = create_onboarding_service(
                config, scm=onboarding_scm, pool=pool
            )
            logger.info("Onboarding service ready (store=%s)", "postgres" if pool else "in-memory")
        except Exception:
            logger.exception("Onboarding service failed to start — Repos page will 503")
            app.state.onboarding_service = None

        # One-shot boot reconcile: rebuild/refresh the onboarding cache from
        # the `.platform/devai.yaml` markers (the source of truth) so the
        # Repos page survives a DB wipe or fresh deploy. DB-first inside
        # reconcile() keeps this cheap on a populated store; the 30s delay
        # lets the SCM client + pool settle first. Endpoint reconcile stays
        # available regardless.
        onboarding_reconcile_task = None
        if app.state.onboarding_service is not None and getattr(
            config, "onboarding_reconcile_on_boot", True
        ):
            async def _boot_reconcile(svc: object) -> None:
                try:
                    await asyncio.sleep(30)
                    report = await svc.reconcile()  # type: ignore[attr-defined]
                    logger.info("Onboarding boot reconcile: %s", report)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    logger.exception("Onboarding boot reconcile failed (non-fatal)")

            onboarding_reconcile_task = asyncio.create_task(
                _boot_reconcile(app.state.onboarding_service)
            )

        try:
            yield
        finally:
            if onboarding_reconcile_task is not None and not onboarding_reconcile_task.done():
                onboarding_reconcile_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await onboarding_reconcile_task
            if onboarding_db is not None:
                try:
                    await onboarding_db.close()
                except Exception:  # noqa: BLE001
                    logger.exception("Onboarding DB close failed")
            if pipeline_service is not None:
                try:
                    await pipeline_service.stop()
                except Exception:  # noqa: BLE001
                    logger.exception("PipelineService stop failed")
            if spec_service is not None:
                try:
                    await spec_service.stop()
                except Exception:  # noqa: BLE001
                    logger.exception("SpecializationService stop failed")

    app = FastAPI(
        title="DevAI",
        version="0.3.0",
        description="AI-powered ALM Pipeline + Fiber-style blueprint runtime",
        lifespan=lifespan,
    )

    # Store shared resources for access in routes
    app.state.event_bus = event_bus
    app.state.event_bus_adapter = event_bus_adapter
    app.state.state_manager = state
    app.state.config = config

    # Opt-in auth gate (DEVAI_REQUIRE_AUTH). No-op unless enabled; when on,
    # mutating requests without a resolvable principal get 401. Webhook
    # routes are exempt (they authenticate via HMAC signature).
    @app.middleware("http")
    async def _auth_gate(request, call_next):
        from devai.authz import enforce_auth

        blocked = await enforce_auth(request)
        if blocked is not None:
            return blocked
        return await call_next(request)

    # NOTE: app.state.registry_client is constructed in lifespan() above
    # alongside SpecializationService so the two share a single client +
    # cache. Set it to None here if the lifespan never ran (some test
    # paths bypass it).
    if not hasattr(app.state, "registry_client"):
        from devai.registry import create_registry_client

        app.state.registry_client = create_registry_client(config)

    # Memory adapter — used by /api/scm/repos/{...}/scan to persist
    # captured repo profiles so future pipeline runs can recall the
    # tech stack without rescanning. Falls back to noop when the
    # backend is unreachable; the SCM route handles both shapes.
    if not hasattr(app.state, "memory_adapter"):
        try:
            from devai.adapters.memory import create_memory_adapter

            app.state.memory_adapter = create_memory_adapter(config)
        except Exception:
            logger.exception("memory adapter construction failed — scan results won't persist")
            app.state.memory_adapter = None

    # Webhook routes
    from devai.webhook.routes import router as webhook_router

    app.include_router(webhook_router)

    # Per-run Repo viewer routes (/api/runs/{run_id}/repo/*) — powers
    # the dashboard's REPO tab. Read-only file tree, file contents, and
    # an SSE stream of live file-change events emitted by the agents.
    from devai.webhook.repo_routes import router as repo_router

    app.include_router(repo_router)

    # Local catalog routes (/api/catalog/*) — exposes the things that
    # don't live in aregistry: built-in tools, blueprints,
    # specializations, registered stages. Augments the upstream registry
    # so the dashboard can render a unified catalog page.
    from devai.tools.routes import router as catalog_router

    app.include_router(catalog_router)

    # Agent Registry catalog routes (/api/registry/*).
    from devai.registry.routes import router as registry_router

    app.include_router(registry_router)

    # Agentic control-plane status (/api/agentic/status + smoke probes).
    # Backs the dashboard's Gateway panel; one endpoint aggregates the
    # health of registry / agentgateway / ai-gateway / kagent.
    from devai.agentic.routes import router as agentic_router

    app.include_router(agentic_router)

    # SCM routes (/api/scm/*) — repo picker + create-new for the New
    # Pipeline Run dialog; issue feed grouped by lane for the
    # Workflows kanban. Reads the PAT from devai-github-pat.
    from devai.scm.routes import router as scm_router

    app.include_router(scm_router)

    # Repo onboarding routes (/api/scm/org/repos + /api/scm/onboarded/*) —
    # backs the Repos page: org catalog with onboarding status, onboard
    # (gated PR), merge/assign-reviewer from inside DevAI, and reconcile
    # from the `.platform/devai.yaml` markers.
    from devai.onboarding.routes import router as onboarding_router

    app.include_router(onboarding_router)

    # Pipeline runtime routes (/api/pipeline/*) — only useful when
    # PipelineService is started, but the routes themselves return a
    # readable 503 when disabled, so we mount unconditionally.
    from devai.pipeline.routes import router as pipeline_router

    app.include_router(pipeline_router)

    # Specializations catalog routes (/api/specializations/*)
    from devai.specializations.routes import router as specializations_router

    app.include_router(specializations_router)

    # Authoring routes (/api/authoring/*) — create custom agents + blueprints.
    from devai.authoring.routes import router as authoring_router

    app.include_router(authoring_router)

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
        """Readiness probe — checks Redis, NATS, and the event-bus adapter."""
        checks: dict[str, str] = {}

        # Redis check
        try:
            await state.redis.ping()
            checks["redis"] = "ok"
        except Exception as e:
            checks["redis"] = f"error: {e}"

        # Legacy NATS connection (used by LangGraph PipelineOrchestrator)
        try:
            if event_bus._nc and not event_bus._nc.is_closed:
                checks["nats"] = "ok"
            else:
                checks["nats"] = "disconnected"
        except Exception as e:
            checks["nats"] = f"error: {e}"

        # Adapter — prefers PipelineService's adapter, falls back to the
        # one passed into create_app() if PipelineService isn't started.
        adapter = None
        ps = getattr(app.state, "pipeline_service", None)
        if ps is not None:
            adapter = getattr(ps, "event_bus_adapter", None)
        if adapter is None:
            adapter = getattr(app.state, "event_bus_adapter", None)
        if adapter is not None:
            try:
                health = await adapter.health_check()
                checks["event_bus_adapter"] = (
                    f"ok ({health.get('provider')})"
                    if health.get("ok")
                    else f"error: {health.get('detail', 'unhealthy')}"
                )
            except Exception as e:
                checks["event_bus_adapter"] = f"error: {e}"

        all_ok = all(v == "ok" or v.startswith("ok ") for v in checks.values())
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
        limit: int = Query(20, ge=1, le=500),
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
    async def get_audit_trail(run_id: str, limit: int = Query(100, ge=1, le=1000)) -> list:
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
