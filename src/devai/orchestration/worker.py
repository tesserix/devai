"""Temporal worker — hosts the generic BlueprintWorkflow + run_stage activity.

Run as the ``devai-worker`` Deployment:

    python -m devai.orchestration.worker

It builds the same StageDeps + StageRegistry as the in-process pipeline (via
:func:`devai.pipeline.bootstrap.build_runtime`), stashes them in the worker
context for the activity, then polls the configured task queue. Because the
workflow + activity are generic, this one worker runs *every* blueprint — adding a
blueprint or agent never requires a worker change.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from devai.config import Settings
from devai.orchestration.activities import publish_progress_activity, run_stage_activity
from devai.orchestration.context import WorkerContext, set_worker_context
from devai.orchestration.workflows import BlueprintWorkflow
from devai.pipeline.bootstrap import build_runtime

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from devai.core.state import StateManager
    from devai.scm.base import SCMClient


async def _build_scm(config: Settings) -> SCMClient | None:
    try:
        from devai.scm.factory import create_scm_client

        return create_scm_client(config)
    except Exception:  # noqa: BLE001
        logger.warning("worker: SCM client unavailable — SCM stages will degrade", exc_info=True)
        return None


async def _build_state_manager(config: Settings) -> StateManager | None:
    try:
        from devai.core.state import StateManager

        sm = StateManager(
            config.redis_url,
            config.redis_result_ttl,
            config.redis_lock_ttl,
        )
        await sm.redis.ping()
        return sm
    except Exception:  # noqa: BLE001
        if config.temporal_worker_dependencies_required:
            logger.exception("worker: required StateManager unavailable")
            raise
        logger.warning("worker: StateManager unavailable — persistence will degrade", exc_info=True)
        return None


def _worker_options(config: Settings) -> dict[str, Any]:
    from temporalio.common import VersioningBehavior, WorkerDeploymentVersion
    from temporalio.worker import WorkerDeploymentConfig

    deployment_config = None
    if config.temporal_worker_versioning_enabled:
        build_id = config.temporal_worker_build_id.strip()
        if not build_id:
            raise ValueError("Temporal worker build ID is required when versioning is enabled")
        deployment_config = WorkerDeploymentConfig(
            version=WorkerDeploymentVersion(
                deployment_name=config.temporal_worker_deployment_name,
                build_id=build_id,
            ),
            use_worker_versioning=True,
            default_versioning_behavior=VersioningBehavior.AUTO_UPGRADE,
        )

    return {
        "max_concurrent_activities": config.temporal_max_concurrent_activities,
        "graceful_shutdown_timeout": timedelta(seconds=config.temporal_worker_graceful_shutdown_seconds),
        "deployment_config": deployment_config,
    }


async def run_worker(config: Settings | None = None) -> None:
    config = config or Settings()

    from temporalio.client import Client
    from temporalio.worker import Worker

    from devai.orchestration.payload_codec import temporal_data_converter

    scm = await _build_scm(config)
    state_manager = await _build_state_manager(config)

    event_bus_adapter = None
    try:
        from devai.adapters.event_bus.factory import create_event_bus_adapter

        event_bus_adapter = create_event_bus_adapter(config)
        await event_bus_adapter.connect()
    except Exception:  # noqa: BLE001
        logger.warning("worker: event-bus unavailable — continuing", exc_info=True)
        event_bus_adapter = None

    bundle = await build_runtime(
        config,
        scm=scm,
        state_manager=state_manager,
        event_bus_adapter=event_bus_adapter,
        registry_client=None,
    )
    set_worker_context(WorkerContext(registry=bundle.registry, deps=bundle.deps))

    host = getattr(config, "temporal_host", "localhost:7233")
    namespace = getattr(config, "temporal_namespace", "default")
    task_queue = getattr(config, "temporal_task_queue", "devai")
    tls = bool(getattr(config, "temporal_tls_enabled", False))

    client = await Client.connect(
        host,
        namespace=namespace,
        tls=tls,
        data_converter=temporal_data_converter(config),
    )
    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[BlueprintWorkflow],
        activities=[run_stage_activity, publish_progress_activity],
        **_worker_options(config),
    )
    logger.info("devai-worker started: host=%s ns=%s queue=%s", host, namespace, task_queue)
    try:
        await worker.run()
    finally:
        await bundle.aclose()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
