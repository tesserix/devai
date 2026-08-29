"""Temporal client boundary for durable Agent development operations."""

from __future__ import annotations

import hashlib
import re
from datetime import timedelta
from typing import Any

from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError

from devai.identity import Principal

_ID_PART = re.compile(r"[^A-Za-z0-9._-]+")


class AgentLifecycleValidationError(ValueError):
    """A workflow rejected immutable input or policy and must not be retried."""


class AgentLifecycleOrchestrator:
    """Start idempotent lifecycle workflows and wait for their public result."""

    def __init__(self, settings: Any, *, client: Any | None = None) -> None:
        self._settings = settings
        self._client = client

    async def import_agent(
        self,
        principal: Principal,
        *,
        project_id: str,
        registry_ref: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        payload = {
            "operation": "import",
            "request": {
                "principal": principal.to_dict(),
                "project_id": project_id,
                "registry_ref": registry_ref,
                "idempotency_key": idempotency_key,
            },
        }
        return await self._run(
            payload,
            workflow_id=_workflow_id("agent-import", principal, project_id, idempotency_key),
        )

    async def evaluate(
        self,
        principal: Principal,
        request: dict[str, Any],
        *,
        request_id: str,
        cleanup: bool = False,
    ) -> dict[str, Any]:
        payload = {
            "operation": "evaluate",
            "request": {
                **request,
                "principal": principal.to_dict(),
                "idempotency_key": request_id,
                "idempotency_scope": "evaluation",
            },
            "cleanup": cleanup,
        }
        project_id = str(request.get("project_id") or "agent-lab")
        return await self._run(
            payload,
            workflow_id=_workflow_id("agent-eval", principal, project_id, request_id),
        )

    async def provision(
        self,
        principal: Principal,
        spec: dict[str, Any],
        *,
        request_id: str,
    ) -> dict[str, Any]:
        payload = {
            "operation": "provision",
            "request": {
                "principal": principal.to_dict(),
                "sandbox": spec,
                "idempotency_key": request_id,
                "idempotency_scope": "sandbox",
            },
        }
        return await self._run(
            payload,
            workflow_id=_workflow_id("agent-sandbox", principal, "agent-lab", request_id),
        )

    async def compare(
        self,
        principal: Principal,
        request: dict[str, Any],
        *,
        request_id: str,
    ) -> dict[str, Any]:
        payload = {
            "operation": "compare",
            "request": {**request, "principal": principal.to_dict(), "idempotency_key": request_id},
        }
        return await self._run(
            payload,
            workflow_id=_workflow_id("agent-compare", principal, "agent-lab", request_id),
        )

    async def promote(
        self,
        principal: Principal,
        request: dict[str, Any],
        *,
        request_id: str,
        requires_approval: bool,
    ) -> dict[str, Any]:
        payload = {
            "operation": "promote",
            "request": {**request, "principal": principal.to_dict(), "idempotency_key": request_id},
            "requires_approval": requires_approval,
        }
        return await self._run(
            payload,
            workflow_id=_workflow_id("agent-promote", principal, "agent-lab", request_id),
        )

    async def cleanup(
        self,
        principal: Principal,
        sandbox_id: str,
        *,
        request_id: str,
    ) -> dict[str, Any]:
        payload = {
            "operation": "cleanup",
            "request": {
                "principal": principal.to_dict(),
                "sandbox_id": sandbox_id,
                "idempotency_key": request_id,
            },
        }
        return await self._run(
            payload,
            workflow_id=_workflow_id("agent-cleanup", principal, "agent-lab", request_id),
        )

    async def _run(self, payload: dict[str, Any], *, workflow_id: str) -> dict[str, Any]:
        client = await self._ensure_client()
        try:
            handle = await client.start_workflow(
                "AgentLifecycleWorkflow",
                {**payload, "workflow_id": workflow_id},
                id=workflow_id,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                task_queue=getattr(self._settings, "temporal_task_queue", "devai"),
                execution_timeout=timedelta(days=7),
            )
        except WorkflowAlreadyStartedError:
            handle = client.get_workflow_handle(workflow_id)
        try:
            result = await handle.result()
        except Exception as error:
            application_error = _application_error(error)
            if application_error is not None and application_error.type in {
                "AgentLifecycleValidationError",
                "AgentLifecyclePolicyError",
            }:
                raise AgentLifecycleValidationError(str(application_error)) from error
            raise
        if not isinstance(result, dict):
            raise RuntimeError("agent lifecycle workflow returned an invalid result")
        return {str(key): value for key, value in result.items()}

    async def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        from temporalio.client import Client

        from devai.orchestration.payload_codec import temporal_data_converter

        self._client = await Client.connect(
            getattr(self._settings, "temporal_host", "localhost:7233"),
            namespace=getattr(self._settings, "temporal_namespace", "default"),
            tls=bool(getattr(self._settings, "temporal_tls_enabled", False)),
            data_converter=temporal_data_converter(self._settings),
        )
        return self._client


def _workflow_id(prefix: str, principal: Principal, project_id: str, key: str) -> str:
    tenant = principal.tenant_id or principal.user_scope_id
    digest = hashlib.sha256(key.encode()).hexdigest()[:24]
    return ":".join(
        (
            prefix,
            _safe_id_part(tenant),
            _safe_id_part(project_id),
            digest,
        )
    )


def _safe_id_part(value: str) -> str:
    cleaned = _ID_PART.sub("-", value.strip()).strip("-")
    return cleaned[:80] or "unknown"


def _application_error(error: BaseException) -> ApplicationError | None:
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ApplicationError):
            return current
        current = current.__cause__
    return None


__all__ = ["AgentLifecycleOrchestrator", "AgentLifecycleValidationError"]
