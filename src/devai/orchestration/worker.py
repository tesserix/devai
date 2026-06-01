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

from devai.config import Settings
from devai.orchestration.activities import run_stage_activity
from devai.orchestration.context import WorkerContext, set_worker_context
from devai.orchestration.workflows import BlueprintWorkflow
from devai.pipeline.bootstrap import build_runtime

logger = logging.getLogger(__name__)


async def _build_scm(config: Settings):
    try:
        from devai.scm.factory import create_scm_client

        return create_scm_client(config)
    except Exception:  # noqa: BLE001
        logger.warning("worker: SCM client unavailable — SCM stages will degrade", exc_info=True)
        return None


async def _build_state_manager(config: Settings):
    try:
        from devai.core.state import create_state_manager

        sm = create_state_manager(config)
        connect = getattr(sm, "connect", None)
        if connect is not None:
            maybe = connect()
            if asyncio.iscoroutine(maybe):
                await maybe
        return sm
    except Exception:  # noqa: BLE001
        logger.warning("worker: StateManager unavailable — persistence will degrade", exc_info=True)
        return None


async def run_worker(config: Settings | None = None) -> None:
    config = config or Settings()

    from temporalio.client import Client
    from temporalio.worker import Worker

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

    client = await Client.connect(host, namespace=namespace, tls=tls)
    max_activities = int(getattr(config, "temporal_max_concurrent_activities", 50))

    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[BlueprintWorkflow],
        activities=[run_stage_activity],
        max_concurrent_activities=max_activities,
    )
    logger.info(
        "devai-worker started: host=%s ns=%s queue=%s", host, namespace, task_queue
    )
    try:
        await worker.run()
    finally:
        await bundle.aclose()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
