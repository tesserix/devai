"""Temporal-backed workflow adapter — durable blueprint execution.

Starts the one generic ``BlueprintWorkflow`` on a Temporal cluster and awaits its
result. The workflow + activity live in :mod:`devai.orchestration` and run in the
``devai-worker`` process; this adapter is the *client* side that the API/pipeline
uses to kick a run off and wait for it.

All ``temporalio`` imports are lazy (inside methods) so importing this module never
requires the SDK. If the cluster is unreachable the adapter **degrades** to the
injected fallback (in-process) rather than failing the request — "degrade, never
crash".
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from .base import WorkflowAdapter

if TYPE_CHECKING:
    from devai.blueprint.loader import Blueprint
    from devai.config import Settings
    from devai.pipeline.types import DevAITask

logger = logging.getLogger(__name__)


class TemporalWorkflowAdapter(WorkflowAdapter):
    """Runs blueprints as durable Temporal workflows."""

    def __init__(self, settings: Settings, *, fallback: WorkflowAdapter) -> None:
        self._settings = settings
        self._fallback = fallback
        self._client: Any = None

    @property
    def provider_name(self) -> str:
        return "temporal"

    async def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        from temporalio.client import Client  # lazy

        host = getattr(
            self._settings, "temporal_host", "localhost:7233"
        )
        namespace = getattr(self._settings, "temporal_namespace", "default")
        tls = bool(getattr(self._settings, "temporal_tls_enabled", False))
        self._client = await Client.connect(host, namespace=namespace, tls=tls)
        logger.info("TemporalWorkflowAdapter connected: %s ns=%s", host, namespace)
        return self._client

    async def run_blueprint(self, blueprint: Blueprint, task: DevAITask) -> DevAITask:
        from devai.orchestration.serde import (
            blueprint_to_dict,
            task_from_dict,
            task_to_dict,
        )

        try:
            client = await self._ensure_client()
        except Exception:  # noqa: BLE001
            logger.exception(
                "Temporal connect failed — degrading to %s for task %s",
                self._fallback.provider_name,
                task.id,
            )
            return await self._fallback.run_blueprint(blueprint, task)

        task_queue = getattr(self._settings, "temporal_task_queue", "devai")
        default_timeout = int(
            getattr(self._settings, "pipeline_default_stage_timeout", 900)
        )
        max_attempts = int(getattr(self._settings, "temporal_max_stage_attempts", 3))
        payload = {
            "blueprint": blueprint_to_dict(blueprint),
            "task": task_to_dict(task),
            "default_stage_timeout": default_timeout,
            "max_stage_attempts": max_attempts,
        }

        try:
            handle = await client.start_workflow(
                "BlueprintWorkflow",
                payload,
                id=f"devai-bp-{task.id}",
                task_queue=task_queue,
            )
            result = await handle.result()
        except Exception:  # noqa: BLE001
            logger.exception(
                "Temporal workflow failed for task %s — degrading to %s",
                task.id,
                self._fallback.provider_name,
            )
            return await self._fallback.run_blueprint(blueprint, task)

        return task_from_dict(result)

    async def health_check(self) -> bool:
        try:
            await self._ensure_client()
            return True
        except Exception:  # noqa: BLE001
            return False
