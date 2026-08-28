"""Build Agent-lab services for the dedicated Temporal worker process."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import Any

from devai.orchestration.agent_lifecycle_activities import AgentLifecycleActivities


@dataclass(slots=True)
class AgentLifecycleRuntime:
    activities: AgentLifecycleActivities
    bundle: Any
    database: Any
    specializations: Any
    object_store: Any
    telemetry: Any

    async def aclose(self) -> None:
        with contextlib.suppress(Exception):
            await self.specializations.stop()
        closer = getattr(self.object_store, "close", None)
        if closer is not None:
            with contextlib.suppress(Exception):
                await closer()
        with contextlib.suppress(Exception):
            await self.database.close()
        await self.bundle.aclose()
        with contextlib.suppress(Exception):
            await self.telemetry.close()
        from devai.adapters.telemetry import set_global_telemetry

        set_global_telemetry(None)


async def build_agent_lifecycle_runtime(
    config: Any,
    *,
    scm: Any = None,
    state_manager: Any = None,
    event_bus_adapter: Any = None,
) -> AgentLifecycleRuntime:
    """Construct the same Registry/sandbox/evaluation boundaries used by the API."""
    from devai.adapters.object_store import create_object_store_adapter
    from devai.adapters.secrets import create_secrets_adapter
    from devai.adapters.telemetry import create_telemetry_adapter, set_global_telemetry
    from devai.evaluations import AgentGateService, EvaluationService
    from devai.evaluations.job import JobEvaluationInvoker
    from devai.evaluations.judge import JudgeFactory
    from devai.kit.versions import create_adk_catalogue
    from devai.pipeline.bootstrap import build_runtime
    from devai.registry import create_registry_client
    from devai.registry.imports import AgentImportService
    from devai.registry.promotion import AgentPromotionService
    from devai.sandbox import SandboxProvisioner, SandboxService
    from devai.sandbox.credentials import SandboxCredentialResolver
    from devai.sandbox.evals import EvalRunner, EvalStore
    from devai.sandbox.invoke import SandboxInvoker
    from devai.sandbox.trace import TraceStore
    from devai.services.database import Database
    from devai.settings.service import SettingsService
    from devai.specializations.service import SpecializationService

    database = Database(config.database_url)
    await database.connect()
    registry = create_registry_client(config)
    if registry is None:
        await database.close()
        raise RuntimeError("Agent Registry is required by the lifecycle worker")
    telemetry = create_telemetry_adapter(config)
    set_global_telemetry(telemetry)

    settings = SettingsService(pool=database.pool, secrets=create_secrets_adapter(config))
    bundle = await build_runtime(
        config,
        scm=scm,
        state_manager=state_manager,
        event_bus_adapter=event_bus_adapter,
        registry_client=registry,
        settings_service=settings,
    )
    specializations = SpecializationService(config, registry_client=registry)
    await specializations.start()
    object_store = create_object_store_adapter(config)
    traces = TraceStore(
        getattr(state_manager, "redis", None),
        object_store=object_store,
    )
    invoker = SandboxInvoker(
        specializations=specializations,
        deps=bundle.deps,
        traces=traces,
        credentials=SandboxCredentialResolver(service=settings),
        telemetry=telemetry,
    )
    evaluation_invoker = JobEvaluationInvoker(
        deps=bundle.deps,
        traces=traces,
        fallback=invoker,
    )
    runner = EvalRunner(
        evaluation_invoker,
        EvalStore(None, database=database),
        max_cases=int(getattr(config, "sandbox_max_eval_cases_per_run", 50) or 50),
        max_concurrency=int(getattr(config, "sandbox_eval_max_concurrency", 4) or 4),
        judge_factory=JudgeFactory(bundle.deps),
    )
    sandboxes = SandboxService(
        database,
        registry=registry,
        settings=config,
        provisioner=(SandboxProvisioner(bundle.k8s_runtime, database) if bundle.k8s_runtime is not None else None),
        adk_catalogue=create_adk_catalogue(config),
    )
    evaluations = EvaluationService(
        database=database,
        object_store=object_store,
        registry=registry,
    )
    promotion = AgentPromotionService(
        registry,
        AgentGateService(database=database, evaluations=evaluations, audit=database.audit),
    )
    activities = AgentLifecycleActivities(
        imports=AgentImportService(database=database, registry=registry),
        sandboxes=sandboxes,
        evaluations=evaluations,
        runner=runner,
        promote=promotion.promote_from_payload,
        events=database,
    )
    return AgentLifecycleRuntime(
        activities=activities,
        bundle=bundle,
        database=database,
        specializations=specializations,
        object_store=object_store,
        telemetry=telemetry,
    )


__all__ = ["AgentLifecycleRuntime", "build_agent_lifecycle_runtime"]
