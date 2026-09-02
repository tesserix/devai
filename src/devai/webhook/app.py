"""FastAPI webhook application factory."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

if TYPE_CHECKING:
    from devai.adapters.event_bus.base import EventBusAdapter
    from devai.config import Settings
    from devai.core.event_bus import EventBus
    from devai.core.state import StateManager
    from devai.pipeline.interfaces import StageDeps

logger = logging.getLogger(__name__)
_START_TIME = time.time()


def _sandbox_stage_deps(app: FastAPI, config: Settings) -> StageDeps:
    """Return service wiring that the sandbox credential resolver will strip."""
    from devai.pipeline.interfaces import StageDeps

    deps = getattr(getattr(app.state, "pipeline_service", None), "stage_deps", None)
    if isinstance(deps, StageDeps):
        return deps

    llm = None
    try:
        from devai.adapters.llm.factory import create_llm_adapter

        llm = create_llm_adapter(config)
    except Exception:  # noqa: BLE001
        logger.debug("sandbox invoker: LLM adapter construction failed", exc_info=True)
    return StageDeps(config=config, llm=llm)


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
        # LangSmith tracing FIRST — the SDK reads LANGCHAIN_* from the
        # process env, but deployments configure DEVAI_LANGCHAIN_*. Without
        # this export+init the API server never traced anything (the CLI and
        # SRE server already do it; this app was the gap).
        try:
            from devai.services.tracing import init_langsmith

            config.export_langsmith_env()
            init_langsmith()
        except Exception:  # noqa: BLE001 — tracing is optional, never blocks startup
            logger.exception("langsmith init failed (non-fatal)")

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
        app.state.agent_import_service = None

        # A2A (Agent2Agent) runtime client — lets the orchestrator discover
        # peer agents via the registry, fetch their capability cards, and
        # invoke them over A2A. Purely additive: None when no registry client
        # exists, and the orchestrator runs fine without it.
        try:
            from devai.a2a import create_a2a_client

            app.state.a2a_client = create_a2a_client(config, _registry_client)
        except Exception:
            logger.exception("A2A client construction failed (non-fatal)")
            app.state.a2a_client = None

        # Settings capability — built early (before the pipeline runtime) so
        # both the pipeline stages and the conversational gateway share ONE
        # SettingsService and resolve the same per-user/per-tenant connectors.
        # The secrets adapter writes values to the backend (GCP SM); the service
        # persists only references in Postgres, with an in-memory fallback when
        # the DB is unreachable. secrets_provider=noop → catalog + non-secret
        # prefs still work; secret writes return a clear 409.
        settings_service = None
        settings_db = None
        app.state.settings_service = None
        app.state.secrets_adapter = None
        if getattr(config, "settings_enabled", True):
            try:
                from devai.adapters.secrets import create_secrets_adapter
                from devai.settings.service import SettingsService

                secrets_adapter = create_secrets_adapter(config)
                app.state.secrets_adapter = secrets_adapter

                settings_pool = None
                try:
                    from devai.services.database import Database

                    settings_db = Database(config.database_url)
                    await settings_db.connect()
                    settings_pool = settings_db.pool
                except Exception as e:  # noqa: BLE001
                    logger.warning("Settings: DB pool unavailable (%s) — using in-memory store", e)
                    settings_db = None

                settings_service = SettingsService(pool=settings_pool, secrets=secrets_adapter)
                app.state.settings_service = settings_service
                logger.info(
                    "Settings service ready (store=%s, secrets=%s)",
                    "postgres" if settings_pool else "in-memory",
                    secrets_adapter.provider_name,
                )
            except Exception:
                logger.exception("Settings service failed to start — settings API will 503")
                settings_service = None
                app.state.settings_service = None

        # Per-user SCM resolution for HTTP surfaces (Repos page). One resolver
        # per app so N requests by the same user share one client + token cache.
        app.state.scm_resolver = None
        if settings_service is not None:
            try:
                from devai.settings.scm_resolver import PrincipalSCMResolver

                app.state.scm_resolver = PrincipalSCMResolver(config, settings_service)
            except Exception:  # noqa: BLE001
                logger.exception("Per-user SCM resolver failed to start — platform SCM only")

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
                    settings_service=settings_service,
                    telemetry=getattr(app.state, "telemetry", None),
                )
                await pipeline_service.start()
                app.state.pipeline_service = pipeline_service
                # Re-point the app-level memory adapter at the pipeline's.
                # The one built in create_app() ran before start() attached
                # the StateManager/Database to config, so under pgvector it
                # degraded to Noop — which silently discarded scan-route
                # writes and made /readyz report the wrong provider.
                pipeline_memory = getattr(pipeline_service, "_memory_adapter", None)
                if pipeline_memory is not None:
                    app.state.memory_adapter = pipeline_memory
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

                spec_service = SpecializationService(config, registry_client=_registry_client)
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
            authoring_service = create_authoring_service(
                redis=redis,
                spec_registry=spec_registry,
                registry_client=_registry_client,
                settings=config,
                pipeline=pipeline_service,
            )
            await authoring_service.load_into_registry()
            if getattr(config, "registry_publish_on_boot", False):
                published = await authoring_service.republish_all()
                if published:
                    logger.info("Authoring: republished %d artifact(s) to the registry on boot", published)
            app.state.authoring_service = authoring_service
            logger.info(
                "Authoring service ready (store=%s, publish=%s)",
                "redis" if redis else "in-memory",
                "on" if getattr(config, "registry_publish_enabled", False) else "off",
            )
        except Exception:
            logger.exception("Authoring service failed to start — authoring API will 503")
            app.state.authoring_service = None

        # SRE Studio — author/dry-run/publish custom SRE blueprints & agents.
        # Drafts live in Postgres (sre_config_drafts); dry-run uses the
        # PipelineService with dry_run=True; publish delegates to the
        # authoring service (hot-register + push to the agentic-registry).
        app.state.sre_studio_db = None
        app.state.sre_studio_service = None
        try:
            from devai.services.database import Database as _StudioDB
            from devai.sre_studio import SREStudioService

            studio_db = _StudioDB(config.database_url)
            await studio_db.connect()
            app.state.sre_studio_db = studio_db
            app.state.sre_studio_service = SREStudioService(
                studio_db,
                pipeline=app.state.pipeline_service,
                authoring=app.state.authoring_service,
            )
            logger.info("SRE Studio service ready")
        except Exception:
            logger.exception("SRE Studio service failed to start — sre-studio API will 503")
            app.state.sre_studio_service = None

        if app.state.sre_studio_db is not None and _registry_client is not None:
            from devai.registry.imports import AgentImportService

            app.state.agent_import_service = AgentImportService(
                database=app.state.sre_studio_db,
                registry=_registry_client,
            )
            logger.info("Registry import service ready")

        # Live preview service — on-demand ephemeral preview environments.
        # Reuses the SRE Studio DB pool + the pipeline's connected K8s runtime.
        app.state.preview_service = None
        try:
            from devai.preview import PreviewService

            if app.state.sre_studio_db is not None:
                # An SCM client lets the resolver read the repo tree + key files
                # to detect the stack (FE+BE+DB); falls back to a Node FE default
                # when absent or detection fails.
                preview_scm = None
                try:
                    from devai.scm import create_scm_client

                    preview_scm = create_scm_client(config)
                except Exception as e:  # noqa: BLE001
                    logger.warning("Preview: SCM client unavailable (%s); detection degrades to FE default", e)
                app.state.preview_service = PreviewService(
                    app.state.sre_studio_db,
                    pipeline=app.state.pipeline_service,
                    settings=config,
                    scm=preview_scm,
                )
                # CODE-11: launch the idle-TTL reaper so abandoned previews
                # don't leak until the namespace quota self-DoSes.
                app.state.preview_service.start_reaper()
                logger.info("Preview service ready (TTL reaper started)")
        except Exception:
            logger.exception("Preview service failed to start — preview API will 503")
            app.state.preview_service = None

        # Agent sandboxes — pinned, TTL-bounded agent configurations (#179).
        app.state.sandbox_service = None
        app.state.adk_catalogue = None
        try:
            from devai.kit.versions import create_adk_catalogue
            from devai.sandbox import SandboxProvisioner, SandboxService

            app.state.adk_catalogue = create_adk_catalogue(config)
            if app.state.sre_studio_db is not None:
                # Reuses the pipeline's connected K8s runtime; without it a
                # sandbox is still recorded, just never fenced in the cluster.
                sandbox_runtime = getattr(app.state.pipeline_service, "k8s_runtime", None)
                app.state.sandbox_service = SandboxService(
                    app.state.sre_studio_db,
                    registry=getattr(app.state, "registry_client", None),
                    settings=config,
                    provisioner=(
                        SandboxProvisioner(sandbox_runtime, app.state.sre_studio_db)
                        if sandbox_runtime is not None
                        else None
                    ),
                    adk_catalogue=app.state.adk_catalogue,
                    runtime=sandbox_runtime,
                )
                app.state.sandbox_service.start_reaper()
                logger.info("Sandbox service ready (TTL reaper started)")
        except Exception:
            logger.exception("Sandbox service failed to start — sandbox API will 503")
            app.state.sandbox_service = None

        # Invoking a sandbox: one turn of the pinned agent, and the trace it
        # leaves behind (Redis, expiring with the sandbox it belongs to).
        app.state.sandbox_traces = None
        app.state.sandbox_invoker = None
        app.state.sandbox_evals = None
        app.state.evaluation_service = None
        app.state.agent_gate_service = None
        try:
            from devai.sandbox.credentials import SandboxCredentialResolver
            from devai.sandbox.evals import EvalRunner, EvalStore
            from devai.sandbox.invoke import SandboxInvoker
            from devai.sandbox.trace import TraceStore

            sandbox_deps = _sandbox_stage_deps(app, config)
            trace_object_store = (sandbox_deps.extra or {}).get("object_store")
            if trace_object_store is None:
                from devai.adapters.object_store import create_object_store_adapter

                trace_object_store = create_object_store_adapter(config)
            app.state.sandbox_traces = TraceStore(
                getattr(state, "redis", None),
                object_store=trace_object_store,
            )
            if app.state.sre_studio_db is not None and getattr(trace_object_store, "provider_name", "noop") != "noop":
                from devai.evaluations import AgentGateService, EvaluationService

                app.state.evaluation_service = EvaluationService(
                    database=app.state.sre_studio_db,
                    object_store=trace_object_store,
                    registry=getattr(app.state, "registry_client", None),
                )
                app.state.agent_gate_service = AgentGateService(
                    database=app.state.sre_studio_db,
                    evaluations=app.state.evaluation_service,
                    audit=app.state.sre_studio_db.audit,
                )
                logger.info("Evaluation dataset service ready (durable metadata + object store)")
            elif app.state.sre_studio_db is not None:
                logger.warning("Evaluation datasets disabled: durable object store unavailable")
            sandbox_audit = None
            if app.state.sre_studio_db is not None:

                async def sandbox_audit(event: dict[str, str]) -> None:
                    details = {key: value for key, value in event.items() if key not in {"action", "owner"}}
                    await app.state.sre_studio_db.audit(
                        action=event["action"],
                        actor=event["owner"],
                        actor_type="user",
                        entity_type="sandbox",
                        entity_ref=event["sandbox_id"],
                        details=details,
                    )

            # The service, not its registry: an agent published from the UI has
            # to be invokable without waiting for the next restart.
            if spec_service is not None:
                app.state.sandbox_invoker = SandboxInvoker(
                    specializations=spec_service,
                    deps=sandbox_deps,
                    traces=app.state.sandbox_traces,
                    registry=getattr(app.state, "registry_client", None),
                    credentials=SandboxCredentialResolver(
                        service=settings_service,
                        audit=sandbox_audit,
                    ),
                )
                if app.state.sre_studio_db is not None:
                    from devai.evaluations.job import JobEvaluationInvoker
                    from devai.evaluations.judge import JudgeFactory

                    evaluation_invoker = JobEvaluationInvoker(
                        deps=sandbox_deps,
                        traces=app.state.sandbox_traces,
                        fallback=app.state.sandbox_invoker,
                    )
                    app.state.sandbox_evals = EvalRunner(
                        evaluation_invoker,
                        EvalStore(None, database=app.state.sre_studio_db),
                        max_cases=int(getattr(config, "sandbox_max_eval_cases_per_run", 50) or 50),
                        max_concurrency=int(getattr(config, "sandbox_eval_max_concurrency", 4) or 4),
                        judge_factory=JudgeFactory(sandbox_deps),
                        telemetry=app.state.telemetry,
                    )
                    logger.info("Evaluation runner ready (backend=%s)", evaluation_invoker.execution_backend)
                logger.info("Sandbox invoker ready")
        except Exception:
            logger.exception("Sandbox invoker failed to start — invoke API will 503")
            app.state.sandbox_invoker = None
            app.state.sandbox_evals = None

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
                # Shared pool reused by other features (e.g. local_db auth reads
                # the seeded devai_local_users table from app.state.db_pool).
                app.state.db_pool = pool
            except Exception as e:  # noqa: BLE001
                logger.warning("Onboarding: DB pool unavailable (%s) — using in-memory store", e)
                onboarding_db = None

            app.state.onboarding_service = create_onboarding_service(config, scm=onboarding_scm, pool=pool)
            logger.info("Onboarding service ready (store=%s)", "postgres" if pool else "in-memory")

            # Teams service shares the onboarding DB pool. When the DB is
            # unavailable it's left None and the teams API returns 503 /
            # principals resolve to no teams — teams stay purely additive.
            try:
                if onboarding_db is not None:
                    from devai.services.teams import TeamService

                    app.state.team_service = TeamService(onboarding_db)
                    logger.info("Team service ready")
                else:
                    app.state.team_service = None
            except Exception:
                logger.exception("Team service failed to start — teams API will 503")
                app.state.team_service = None
        except Exception:
            logger.exception("Onboarding service failed to start — Repos page will 503")
            app.state.onboarding_service = None

        # Onboarding reconcile poller: rebuild/refresh the cache from the
        # `.platform/devai.yaml` markers (the source of truth) so the Repos
        # page survives a DB wipe or fresh deploy, and so markers added or
        # removed out-of-band are picked up automatically. The boot pass runs
        # ~30s after start (lets the SCM client + pool settle); after that the
        # poller re-runs every onboarding_reconcile_interval_seconds. DB-first
        # reconcile() folds the whole org into one batched GraphQL probe, so
        # each pass is cheap. Endpoint reconcile stays available regardless.
        onboarding_reconcile_task = None
        if app.state.onboarding_service is not None and getattr(config, "onboarding_reconcile_on_boot", True):
            reconcile_interval = max(0, int(getattr(config, "onboarding_reconcile_interval_seconds", 300)))

            async def _reconcile_poller(svc: object, interval: int) -> None:
                await asyncio.sleep(30)  # let the SCM client + pool settle
                while True:
                    try:
                        report = await svc.reconcile()  # type: ignore[attr-defined]
                        logger.info("Onboarding reconcile: %s", report)
                    except asyncio.CancelledError:
                        raise
                    except Exception:  # noqa: BLE001
                        logger.exception("Onboarding reconcile failed (non-fatal)")
                    if interval <= 0:
                        return  # one-shot mode — boot reconcile only
                    await asyncio.sleep(interval)

            onboarding_reconcile_task = asyncio.create_task(
                _reconcile_poller(app.state.onboarding_service, reconcile_interval)
            )

        # Memory lifecycle maintenance — periodic TTL purge / decay / dedup
        # over the pgvector store. Single-replica service (KEDA pins 1), so
        # an in-process loop is the simplest correct scheduler. No-op when
        # Postgres is unreachable or DEVAI_MEMORY_MAINTENANCE_HOURS=0.
        memory_maintenance_task = None
        maintenance_hours = max(0, int(getattr(config, "memory_maintenance_hours", 6)))
        if maintenance_hours > 0:

            async def _memory_maintenance_loop(hours: int) -> None:
                await asyncio.sleep(120)  # let pools settle after boot
                from devai.adapters.memory.maintenance import run_memory_maintenance
                from devai.services.database import Database

                db: Database | None = None
                while True:
                    try:
                        if db is None and getattr(config, "database_url", ""):
                            db = Database(config.database_url)
                            await db.connect()
                        await run_memory_maintenance(
                            db,
                            episodic_ttl_days=int(getattr(config, "memory_episodic_ttl_days", 90)),
                            decay_idle_days=int(getattr(config, "memory_decay_idle_days", 30)),
                            dedup_similarity=float(getattr(config, "memory_dedup_similarity", 0.95)),
                        )
                    except asyncio.CancelledError:
                        if db is not None:
                            with suppress(Exception):
                                await db.close()
                        raise
                    except Exception:  # noqa: BLE001
                        logger.exception("memory maintenance pass failed (non-fatal)")
                        db = None  # reconnect on the next pass
                    await asyncio.sleep(hours * 3600)

            memory_maintenance_task = asyncio.create_task(_memory_maintenance_loop(maintenance_hours))

        # Autonomous backlog watcher — polls every ONBOARDED repo's open issues
        # and dispatches a pipeline run for each new one (DEVAI_ISSUE_WATCH_ENABLED,
        # off by default). Reactive webhooks still work; this adds the unprompted
        # "connect + monitor + auto-detect" path the platform was missing.
        issue_watch_task = None
        if (
            getattr(config, "issue_watch_enabled", False)
            and app.state.onboarding_service is not None
            and app.state.pipeline_service is not None
        ):
            from devai.onboarding.watcher import IssueWatcher
            from devai.scm import create_scm_client

            _watch_redis = getattr(getattr(app.state.pipeline_service, "state_manager", None), "redis", None)
            issue_watcher = IssueWatcher(
                onboarding=app.state.onboarding_service,
                scm=create_scm_client(config),
                pipeline=app.state.pipeline_service,
                redis=_watch_redis,
                config=config,
            )
            issue_watch_task = asyncio.create_task(issue_watcher.run_forever())
            logger.info(
                "Issue watcher enabled (interval=%ss, max/repo=%s)",
                getattr(config, "issue_watch_interval_seconds", 300),
                getattr(config, "issue_watch_max_per_repo", 3),
            )

        # Per-domain downstream MCP servers (/mcp/scm, /mcp/sample) the MCP Hub
        # federates. Mounted BEFORE the messaging /mcp mount: Starlette is
        # first-match-wins and Mount("/mcp") greedily matches "/mcp/scm" as a
        # prefix, so these more-specific mounts must register first or they're
        # shadowed (the request would 404 inside the messaging app). Flag-gated;
        # additive; a failure here never blocks startup.
        app.state._domain_mcp_cms = []
        if getattr(config, "mcp_downstream_servers_enabled", False):
            try:
                from devai.mcphub.tool_server import mount_domain_servers

                app.state._domain_mcp_cms = await mount_domain_servers(app, config)
            except Exception:
                logger.exception("downstream domain MCP mount failed — skipped")

        # Messaging service — remote conversational channels (Slack, remote
        # URL/thread, MCP server). All three are thin transports over one
        # ConversationGateway; the service owns the channel map + the NATS turn
        # worker. Reuses the onboarding DB for audit and the settings service
        # for per-user overlays. Purely additive: if every channel is disabled
        # it builds nothing.
        messaging_service = None
        app.state.messaging_service = None
        try:
            from devai.chat.messaging_service import MessagingService

            messaging_service = MessagingService(
                config,
                state,
                database=onboarding_db,
                event_bus_adapter=event_bus_adapter,
                settings_service=settings_service,
            )
            await messaging_service.start()
            app.state.messaging_service = messaging_service

            # Mount the MCP server sub-app (Streamable HTTP) when enabled and
            # the SDK is present. Shares this app's ingress + port at /mcp.
            if "mcp" in messaging_service.channels:
                try:
                    from devai.adapters.messaging.mcp import build_mcp_server

                    mcp_server = build_mcp_server(messaging_service)
                    app.state.mcp_server = mcp_server
                    app.mount("/mcp", mcp_server.streamable_http_app())
                    # The streamable-http app needs its session manager running.
                    app.state._mcp_session_cm = mcp_server.session_manager.run()
                    await app.state._mcp_session_cm.__aenter__()
                    logger.info("MCP server mounted at /mcp")
                except Exception:
                    logger.exception("MCP mount failed — MCP channel disabled")
                    app.state.mcp_server = None
        except Exception:
            logger.exception("MessagingService failed to start — remote channels disabled")
            app.state.messaging_service = None

        # Admin analytics rollups reuse this same Postgres handle. Connected
        # here (not lazily on first request) so `record_active` — which only
        # writes when app.state.analytics_db is already set — never silently
        # drops active-user rows before the first /api/analytics/* call.
        app.state.analytics_db = None
        if getattr(config, "database_url", ""):
            try:
                from devai.services.database import Database

                analytics_db = Database(config.database_url)
                await analytics_db.connect()
                app.state.analytics_db = analytics_db
            except Exception:  # noqa: BLE001
                logger.info("analytics: Postgres unavailable at startup — rollups disabled", exc_info=True)
                app.state.analytics_db = None

        try:
            yield
        finally:
            if getattr(app.state, "_mcp_session_cm", None) is not None:
                with suppress(Exception):
                    await app.state._mcp_session_cm.__aexit__(None, None, None)
            for cm in getattr(app.state, "_domain_mcp_cms", []) or []:
                with suppress(Exception):
                    await cm.__aexit__(None, None, None)
            preview_service = getattr(app.state, "preview_service", None)
            if preview_service is not None:
                with suppress(Exception):
                    await preview_service.stop_reaper()
            sandbox_service = getattr(app.state, "sandbox_service", None)
            if sandbox_service is not None:
                with suppress(Exception):
                    await sandbox_service.stop_reaper()
            if messaging_service is not None:
                with suppress(Exception):
                    await messaging_service.stop()
            if onboarding_reconcile_task is not None and not onboarding_reconcile_task.done():
                onboarding_reconcile_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await onboarding_reconcile_task
            if memory_maintenance_task is not None:
                memory_maintenance_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await memory_maintenance_task
            if issue_watch_task is not None:
                issue_watch_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await issue_watch_task
            if onboarding_db is not None:
                try:
                    await onboarding_db.close()
                except Exception:  # noqa: BLE001
                    logger.exception("Onboarding DB close failed")
            if settings_db is not None:
                with suppress(Exception):
                    await settings_db.close()
            studio_db = getattr(app.state, "sre_studio_db", None)
            if studio_db is not None:
                with suppress(Exception):
                    await studio_db.close()
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
            telemetry = getattr(app.state, "telemetry", None)
            if telemetry is not None:
                with suppress(Exception):
                    await telemetry.close()  # flush the OTLP exporter
            analytics_db = getattr(app.state, "analytics_db", None)
            if analytics_db is not None:
                with suppress(Exception):
                    await analytics_db.close()

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
    app.state.agent_lifecycle_orchestrator = None
    if getattr(config, "workflow_provider", "inproc") == "temporal":
        from devai.orchestration.agent_lifecycle_client import AgentLifecycleOrchestrator

        app.state.agent_lifecycle_orchestrator = AgentLifecycleOrchestrator(config)

    # Daily active-user recording. Starlette runs middleware in the reverse
    # of add order, so registering this before the auth gate below makes it
    # the innermost layer — it only runs once auth has resolved the
    # request, and its failures are swallowed so a telemetry miss can never
    # fail a user request.
    from devai.admin.activity import ActivityMiddleware

    app.add_middleware(ActivityMiddleware)

    # Dedup guard for the above — one audit_log row per user per day, shared
    # across pods. None degrades to "record nothing", never to a crash.
    app.state.activity_redis = None
    try:
        import redis.asyncio as _redis

        url = getattr(config, "redis_url", "") or ""
        if url:
            app.state.activity_redis = _redis.from_url(url, decode_responses=True)
    except Exception:  # noqa: BLE001
        logger.info("activity dedup guard unavailable — active-user stats disabled")

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

    # Bound the request body before any handler reads it. Added last of the
    # two so it sits outside the auth gate — an oversized body is rejected
    # without buffering it for an identity lookup.
    from devai.services.request_limits import BodySizeLimitMiddleware

    app.add_middleware(BodySizeLimitMiddleware)

    # Telemetry adapter (adapters/telemetry). Built synchronously HERE — not in
    # lifespan — because instrument_asgi adds middleware, which must be
    # registered before the app starts serving. Added after the auth gate so
    # the request span/metrics wrap the whole handler (auth included). Noop
    # unless DEVAI_TELEMETRY_PROVIDER=otel + DEVAI_OTEL_ENDPOINT are set; the
    # factory never raises, so a bad config degrades to Noop, not a crash.
    # In-process log ring — backs the dashboard's live Logs view
    # (/api/analytics/logs). Bounded memory, no I/O; durable history is the
    # GCS archive CronJob in tesserix-k8s.
    try:
        from devai.services.log_buffer import install as install_log_buffer

        install_log_buffer(int(getattr(config, "log_buffer_capacity", 2000)))
    except Exception:  # noqa: BLE001
        logger.exception("log ring install failed — live Logs view disabled")

    try:
        from devai.adapters.telemetry import create_telemetry_adapter, set_global_telemetry

        app.state.telemetry = create_telemetry_adapter(config)
        app.state.telemetry.instrument_asgi(app)
        # Register as the process-global sink so call sites without
        # constructor injection (the instrumented LLM delegate, tools)
        # emit into the same exporter.
        set_global_telemetry(app.state.telemetry)
    except Exception:  # noqa: BLE001
        logger.exception("telemetry adapter construction failed — continuing without telemetry")
        app.state.telemetry = None

    # LLM usage ledger (Redis) — queryable cost/tokens/latency per model and
    # per user for the analytics page. Fed by the instrumented LLM adapter so
    # it captures every call from every run (blueprint runs don't write
    # agent_executions). Degrades to no-op without Redis.
    try:
        from devai.analytics.usage_ledger import UsageLedger, set_global_ledger

        async def _sandbox_spend_alert(event: dict[str, Any]) -> None:
            database = getattr(app.state, "sre_studio_db", None)
            if database is None:
                return
            await database.audit(
                action=str(event["action"]),
                actor="system:usage-ledger",
                actor_type="system",
                entity_type="tenant",
                entity_ref=str(event["tenant_id"]),
                details={key: value for key, value in event.items() if key not in {"action", "tenant_id"}},
            )

        app.state.usage_ledger = UsageLedger(
            getattr(config, "redis_url", "") or "",
            sandbox_monthly_cost_limit_usd=float(getattr(config, "sandbox_monthly_cost_limit_usd", 100.0) or 0.0),
            sandbox_spend_alert_ratio=float(getattr(config, "sandbox_spend_alert_ratio", 0.8) or 0.0),
            alert_sink=_sandbox_spend_alert,
        )
        set_global_ledger(app.state.usage_ledger)
    except Exception:  # noqa: BLE001
        logger.exception("usage ledger init failed — analytics cost views may be empty")
        app.state.usage_ledger = None

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
            from devai.adapters.memory.runtime import set_global_memory

            app.state.memory_adapter = create_memory_adapter(config)
            set_global_memory(app.state.memory_adapter)
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
    from devai.registry.import_routes import router as registry_import_router
    from devai.registry.routes import router as registry_router

    app.include_router(registry_router)
    app.include_router(registry_import_router)

    # Authenticated Agent2Agent server endpoint for the specialization catalog.
    from devai.a2a.routes import router as a2a_router
    from devai.a2a.routes import well_known_router as a2a_well_known_router

    app.include_router(a2a_router)
    app.include_router(a2a_well_known_router)

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

    # Analytics routes (/api/analytics/*) — read-only rollups over the pipeline
    # runtime (runs/stages), agent_executions (agents/LLM cost), the SRE tables,
    # and telemetry/Prometheus health. Backs the dashboard /analytics page.
    from devai.analytics.routes import router as analytics_router

    app.include_router(analytics_router)

    # Admin routes (/api/admin/*) — platform-owner view of who uses DevAI.
    # Gated by a router-level admin-role dependency, not by the edge, so the
    # boundary holds regardless of how the pod is reached.
    from devai.admin.routes import router as admin_router

    app.include_router(admin_router)

    # Specializations catalog routes (/api/specializations/*)
    from devai.specializations.routes import router as specializations_router

    app.include_router(specializations_router)

    # Authoring routes (/api/authoring/*) — create custom agents + blueprints.
    from devai.authoring.routes import router as authoring_router

    app.include_router(authoring_router)

    # SRE Studio routes (/api/sre-studio/*) — author/dry-run/publish SRE
    # blueprints & agents. 503s until sre_studio_service is wired (lifespan).
    from devai.sre_studio.routes import router as sre_studio_router

    app.include_router(sre_studio_router)

    # Live preview routes (/api/preview/*) — start/inspect/stop on-demand
    # preview environments. 503s until preview_service is wired (lifespan).
    from devai.preview.routes import router as preview_router

    app.include_router(preview_router)

    # Sandbox routes (/api/sandboxes/*) — pinned, TTL-bounded agent
    # configurations. 503s until sandbox_service is wired (lifespan).
    from devai.sandbox.routes import router as sandbox_router
    from devai.sandbox.routes import trace_router

    app.include_router(sandbox_router)
    app.include_router(trace_router)

    # Versioned evaluation datasets and suites. The routes are mounted even
    # when storage is unavailable so callers receive an explicit 503.
    from devai.evaluations.routes import (
        comparison_router,
    )
    from devai.evaluations.routes import (
        router as evaluation_router,
    )

    app.include_router(evaluation_router)
    app.include_router(comparison_router)

    # Runtime version picker (/api/adk/versions) — what a sandbox may pin to.
    from devai.kit.routes import router as adk_router

    app.include_router(adk_router)

    # Model picker (/api/models) — providers and the models each can serve.
    from devai.catalog.routes import router as catalog_router

    app.include_router(catalog_router)

    # Teams routes (/api/teams/*) — human teams + the AI crews they own.
    # Always mounted; returns 503 until team_service is wired (lifespan).
    if not hasattr(app.state, "team_service"):
        app.state.team_service = None
    from devai.teams.routes import router as teams_router

    app.include_router(teams_router)

    # Dashboard routes (UI + API)
    from devai.dashboard.routes import router as dashboard_router

    app.include_router(dashboard_router)

    # Local username/password auth at /auth. The adapter decides the mode:
    # local_db actually checks passwords (kind sandbox); every other provider
    # resolves to Noop ({"mode":"gip"} + 401), and in prod /auth/* is routed
    # to the auth-bff anyway — so mounting unconditionally is safe.
    from devai.dashboard.local_auth_routes import router as local_auth_router

    app.include_router(local_auth_router)

    # Chat routes (chatbot API + WebSocket)
    from devai.chat.routes import router as chat_router

    app.include_router(chat_router)

    # Remote conversational routes (/remote/threads/*) — talk to DevAI from any
    # remote app/URL over a plain HTTP/SSE thread, guarded by a static API
    # token. Routes return 503 when the remote channel is disabled.
    from devai.chat.remote_routes import router as remote_chat_router

    app.include_router(remote_chat_router)

    # Slack Events API route (/webhook/slack) — @mention or DM the DevAI bot.
    # Verifies the Slack signature; acks fast and replies in-thread via the
    # messaging worker. No-ops cleanly when Slack is disabled.
    from devai.chat.slack_routes import router as slack_router

    app.include_router(slack_router)

    # Settings routes (/api/settings/*) — per-user/per-tenant connectors +
    # secret provisioning. Returns 503 until settings_service is wired
    # (lifespan). Each endpoint is Principal-gated.
    from devai.settings.routes import router as settings_router

    app.include_router(settings_router)

    # MCP OAuth (Connect-with-OAuth for hosted SaaS MCP servers). The callback
    # is a GET redirect from the provider carrying the user's session cookie.
    from devai.settings.oauth_routes import router as mcp_oauth_router

    app.include_router(mcp_oauth_router)

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

        # Memory adapter — reported for visibility but never fails readiness:
        # memory degrades to noop by design and must not block rollout.
        memory_adapter = getattr(app.state, "memory_adapter", None)
        if memory_adapter is not None:
            try:
                mem_health = await memory_adapter.health_check()
                checks["memory"] = (
                    f"ok ({mem_health.get('provider')})"
                    if mem_health.get("ok")
                    else f"degraded: {mem_health.get('detail', 'unhealthy')}"
                )
            except Exception as e:
                checks["memory"] = f"degraded: {e}"

        # Visibility-only integration surface (never gates readiness): the
        # active LLM provider, the durable-workflow backend, and the Redis
        # work-queue depth — one curl shows whether every backbone piece is
        # the one you think it is.
        visibility_only = {"memory", "llm", "workflow", "queue"}
        llm_adapter = getattr(ps, "_llm_adapter", None) if ps is not None else None
        if llm_adapter is not None:
            checks["llm"] = f"ok ({getattr(llm_adapter, 'provider_name', 'unknown')})"
        else:
            checks["llm"] = f"configured ({getattr(config, 'llm_provider', 'unset')})"
        checks["workflow"] = f"configured ({getattr(config, 'workflow_provider', 'inproc')})"
        try:
            depth = await state.redis.llen("devai:pipeline:queue")
            processing = await state.redis.llen("devai:pipeline:processing")
            checks["queue"] = f"ok (queued={depth}, processing={processing})"
        except Exception as e:
            checks["queue"] = f"degraded: {e}"

        all_ok = all(v == "ok" or v.startswith("ok ") for k, v in checks.items() if k not in visibility_only)
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
        """List agent memories via the configured MemoryAdapter (pgvector in
        prod) — previously read the legacy Redis store regardless of provider."""
        from devai.adapters.memory.runtime import get_global_memory

        records = await get_global_memory().recall(
            agent=agent or None,
            repo=repo or None,
            memory_type=memory_type or None,
            limit=limit,
        )
        return [r.to_dict() for r in records]

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
