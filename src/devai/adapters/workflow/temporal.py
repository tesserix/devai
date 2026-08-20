"""Temporal-backed workflow adapter — durable blueprint execution.

Starts the one generic ``BlueprintWorkflow`` on a Temporal cluster and awaits its
result. The workflow + activity live in :mod:`devai.orchestration` and run in the
``devai-worker`` process; this adapter is the *client* side that the API/pipeline
uses to kick a run off and wait for it.

All ``temporalio`` imports are lazy (inside methods) so importing this module never
requires the SDK. Local development can degrade before a workflow is started;
production fails closed so a partially accepted run is never repeated in-process.
"""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING, Any

from devai.pipeline.types import TaskState

from .base import WorkflowAdapter

if TYPE_CHECKING:
    from devai.blueprint.loader import Blueprint
    from devai.config import Settings
    from devai.pipeline.types import DevAITask

logger = logging.getLogger(__name__)


def workflow_id_for_task(task: DevAITask) -> str:
    principal = task.principal or {}
    tenant = str(principal.get("tenant_id") or principal.get("pool") or "").strip()
    subject = str(principal.get("uid") or principal.get("email") or task.triggered_by or "system").strip()
    scope = hashlib.sha256(f"{tenant}:{subject}".encode()).hexdigest()[:20]
    return f"devai-bp-{scope}-{task.id}"


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

        from devai.orchestration.payload_codec import temporal_data_converter

        host = getattr(self._settings, "temporal_host", "localhost:7233")
        namespace = getattr(self._settings, "temporal_namespace", "default")
        tls = bool(getattr(self._settings, "temporal_tls_enabled", False))
        self._client = await Client.connect(
            host,
            namespace=namespace,
            tls=tls,
            data_converter=temporal_data_converter(self._settings),
        )
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
            if bool(getattr(self._settings, "temporal_fail_closed", False)):
                logger.exception("Temporal connect failed for task %s", task.id)
                task.error = "durable workflow backend unavailable"
                task.transition(TaskState.STAGE_FAILED)
                return task
            logger.exception("Temporal connect failed for task %s; using local fallback", task.id)
            return await self._fallback.run_blueprint(blueprint, task)

        task_queue = getattr(self._settings, "temporal_task_queue", "devai")
        default_timeout = int(getattr(self._settings, "pipeline_default_stage_timeout", 900))
        max_attempts = int(getattr(self._settings, "temporal_max_stage_attempts", 3))
        payload = {
            "blueprint": blueprint_to_dict(blueprint),
            "task": task_to_dict(task),
            "default_stage_timeout": default_timeout,
            "max_stage_attempts": max_attempts,
        }

        workflow_id = workflow_id_for_task(task)
        try:
            from temporalio.exceptions import WorkflowAlreadyStartedError

            handle = await client.start_workflow(
                "BlueprintWorkflow",
                payload,
                id=workflow_id,
                task_queue=task_queue,
            )
        except WorkflowAlreadyStartedError:
            handle = client.get_workflow_handle(workflow_id)
        except Exception:  # noqa: BLE001
            logger.exception("Temporal workflow start failed for task %s", task.id)
            task.error = "durable workflow start failed"
            task.transition(TaskState.STAGE_FAILED)
            return task

        try:
            result = await handle.result()
        except Exception:  # noqa: BLE001
            logger.exception("Temporal workflow execution failed for task %s", task.id)
            task.error = "durable workflow execution failed"
            task.transition(TaskState.STAGE_FAILED)
            return task

        return task_from_dict(result)

    async def signal(
        self,
        task: DevAITask | str,
        signal_name: str,
        args: list[Any] | None = None,
    ) -> bool:
        """Deliver a durable Signal to the run's BlueprintWorkflow.

        Returns False if the cluster is unreachable or the run is not found.
        """
        task_id = task.id if not isinstance(task, str) else task
        workflow_id = workflow_id_for_task(task) if not isinstance(task, str) else f"devai-bp-{task}"
        try:
            client = await self._ensure_client()
            handle = client.get_workflow_handle(workflow_id)
            await handle.signal(signal_name, *(args or []))
            return True
        except Exception:  # noqa: BLE001
            logger.warning("Temporal signal %s failed for task %s", signal_name, task_id, exc_info=True)
            return False

    async def health_check(self) -> bool:
        try:
            await self._ensure_client()
            return True
        except Exception:  # noqa: BLE001
            return False
