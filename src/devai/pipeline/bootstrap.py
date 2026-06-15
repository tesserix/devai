"""Shared pipeline-runtime bootstrap.

Both the in-process :class:`~devai.pipeline.service.PipelineService` and the
Temporal worker need an *identically wired* :class:`StageDeps` (the same memory /
LLM adapters, the same specialization registry, seed crews, capability adapters,
K8s runtime and aregistry client) plus a :class:`StageRegistry` with all default
stages. Wiring it in one place is what guarantees a stage behaves the same whether
it runs inline or inside a durable Temporal activity — which is the whole point of
"works with any blueprint on any backend".

The event-bus adapter is *injected* (the caller owns its lifecycle / connection),
everything else is constructed here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from devai.adapters.llm.factory import create_llm_chain
from devai.adapters.memory import create_memory_adapter
from devai.blueprint.registry import StageRegistry, register_defaults
from devai.pipeline.interfaces import StageDeps

if TYPE_CHECKING:
    from devai.adapters.event_bus.base import EventBusAdapter
    from devai.adapters.llm.base import LLMAdapter
    from devai.adapters.memory.base import MemoryAdapter
    from devai.config import Settings
    from devai.core.event_bus import EventBus
    from devai.core.state import StateManager
    from devai.scm.base import SCMClient

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RuntimeBundle:
    """Everything needed to run blueprints, plus the handles to clean up."""

    deps: StageDeps
    registry: StageRegistry
    # Handles the caller must close/stop on shutdown.
    memory_adapter: MemoryAdapter | None = None
    llm_adapter: LLMAdapter | None = None
    k8s_runtime: Any = None
    job_watcher: Any = None
    extra: dict[str, Any] = field(default_factory=dict)

    async def aclose(self) -> None:
        """Best-effort teardown of everything this bundle owns."""
        for name in ("job_watcher", "k8s_runtime", "memory_adapter", "llm_adapter"):
            obj = getattr(self, name, None)
            if obj is None:
                continue
            closer = getattr(obj, "stop", None) or getattr(obj, "close", None)
            if closer is None:
                continue
            try:
                await closer()
            except Exception:  # noqa: BLE001
                logger.exception("bootstrap: closing %s failed", name)


async def build_runtime(
    config: Settings,
    *,
    scm: SCMClient | None = None,
    state_manager: StateManager | None = None,
    event_bus: EventBus | None = None,
    event_bus_adapter: EventBusAdapter | None = None,
    registry_client: Any = None,
    settings_service: Any = None,
) -> RuntimeBundle:
    """Construct StageDeps + StageRegistry with every adapter/extra wired in.

    Mirrors what ``PipelineService.start()`` assembles, factored out so the
    Temporal worker shares the exact same wiring. Never raises for optional
    subsystems — each degrades to None/Noop and the pipeline keeps running.
    """
    memory_adapter = create_memory_adapter(config)
    llm_adapter = create_llm_chain(config)

    # Register as the process-global memory so call sites without StageDeps
    # injection (chat tools, legacy agents, dashboard routes) honor the same
    # configured provider instead of constructing their own Redis-only store.
    try:
        from devai.adapters.memory.runtime import set_global_memory

        set_global_memory(memory_adapter)
    except Exception:  # noqa: BLE001
        logger.exception("bootstrap: global memory registration failed")

    # Register the usage ledger here too (not just in webhook/app.py): the
    # Temporal/queue worker and in-process pipeline both build their runtime
    # through this path, so this is what makes the instrumented LLM adapter
    # capture cost/tokens for PIPELINE runs — without it, only chat (api
    # process) was recorded and the analytics cost views stayed at $0.
    try:
        from devai.analytics.usage_ledger import UsageLedger, get_global_ledger, set_global_ledger

        if get_global_ledger() is None:
            set_global_ledger(UsageLedger(getattr(config, "redis_url", "") or ""))
    except Exception:  # noqa: BLE001
        logger.exception("bootstrap: usage ledger registration failed")

    # Secrets backend — used (with settings_service) by stages to resolve a
    # per-user settings overlay at execution time. Degrades to Noop.
    secrets_adapter = None
    try:
        from devai.adapters.secrets import create_secrets_adapter

        secrets_adapter = create_secrets_adapter(config)
    except Exception:  # noqa: BLE001
        logger.exception("bootstrap: secrets adapter init failed")

    extra: dict[str, Any] = {}

    # Specialization catalog (YAML role personas) ----------------------------
    if getattr(config, "specializations_enabled", True):
        try:
            from devai.specializations.registry import SpecializationRegistry

            spec_registry = SpecializationRegistry.from_directory(
                getattr(config, "specializations_dir", "specializations")
            )
            extra["specialization_registry"] = spec_registry
            logger.info("bootstrap: loaded %d specializations", len(spec_registry))
        except Exception:  # noqa: BLE001
            logger.exception("bootstrap: specialization registry load failed")

    # K8s Job runtime + watcher ---------------------------------------------
    k8s_runtime = None
    job_watcher = None
    try:
        from devai.runtime import JobWatcher, create_runtime

        k8s_runtime = await create_runtime(config)
        if k8s_runtime is not None:
            job_watcher = JobWatcher(k8s_runtime)
            await job_watcher.start()
            logger.info(
                "bootstrap: k8s runtime active (namespace=%s)",
                k8s_runtime.config.namespace,
            )
    except Exception:  # noqa: BLE001
        logger.exception("bootstrap: k8s runtime init failed — continuing without it")
        k8s_runtime = None
        job_watcher = None

    if k8s_runtime is not None:
        extra["k8s_runtime"] = k8s_runtime
    if job_watcher is not None:
        extra["job_watcher"] = job_watcher
    if registry_client is not None:
        extra["registry_client"] = registry_client

    # Capability adapters (web search + object store) -----------------------
    try:
        from devai.adapters.object_store import create_object_store_adapter
        from devai.adapters.web_search import create_web_search_adapter

        extra["web_search"] = create_web_search_adapter(config)
        extra["object_store"] = create_object_store_adapter(config)
    except Exception:  # noqa: BLE001
        logger.exception("bootstrap: capability adapter init failed")

    # Seed crews (crews/*.yaml) ---------------------------------------------
    try:
        from devai.crews.loader import load_seed_crews

        extra["seed_crews"] = load_seed_crews(getattr(config, "crews_dir", "crews"))
    except Exception:  # noqa: BLE001
        logger.exception("bootstrap: seed crew load failed")

    # Per-principal LLM resolution: runs triggered by a user whose Settings
    # configure an LLM connector execute on THEIR provider/keys, isolated
    # per tenant. None service → resolver returns None → deps.llm default.
    llm_resolver = None
    scm_resolver = None
    if settings_service is not None:
        from devai.settings.llm_resolver import PrincipalLLMResolver
        from devai.settings.scm_resolver import PrincipalSCMResolver

        llm_resolver = PrincipalLLMResolver(config, settings_service)
        # Per-principal SCM: a run triggered by a user with a Source Control
        # connector talks to git via THEIR PAT / GitHub App, not the global App.
        scm_resolver = PrincipalSCMResolver(config, settings_service)

    deps = StageDeps(
        config=config,
        scm=scm,
        state_manager=state_manager,
        event_bus=event_bus,
        memory=memory_adapter,
        llm=llm_adapter,
        event_bus_adapter=event_bus_adapter,
        secrets=secrets_adapter,
        settings_service=settings_service,
        llm_resolver=llm_resolver,
        scm_resolver=scm_resolver,
        extra=extra or None,
    )

    registry = StageRegistry()
    register_defaults(registry)

    return RuntimeBundle(
        deps=deps,
        registry=registry,
        memory_adapter=memory_adapter,
        llm_adapter=llm_adapter,
        k8s_runtime=k8s_runtime,
        job_watcher=job_watcher,
        extra=extra,
    )
