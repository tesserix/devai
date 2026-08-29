"""Durable orchestration for imported-agent development and evaluation."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

_PATCH = "agent-lifecycle-v1"
_NON_RETRYABLE = [
    "AgentLifecycleValidationError",
    "AgentLifecyclePolicyError",
    "AgentLifecycleSideEffectError",
]
_SHORT_TIMEOUT = timedelta(minutes=2)
_PROVISION_TIMEOUT = timedelta(minutes=10)
_EVALUATION_TIMEOUT = timedelta(hours=2)
_CLEANUP_TIMEOUT = timedelta(minutes=15)


def _retry_policy() -> RetryPolicy:
    return RetryPolicy(
        initial_interval=timedelta(seconds=1),
        backoff_coefficient=2,
        maximum_interval=timedelta(seconds=30),
        maximum_attempts=5,
        non_retryable_error_types=_NON_RETRYABLE,
    )


@workflow.defn(name="AgentLifecycleWorkflow")
class AgentLifecycleWorkflow:
    """Run one import/evaluate/promote/cleanup operation as a durable saga."""

    def __init__(self) -> None:
        self._workflow_id = ""
        self._principal: dict[str, Any] = {}
        self._transition_sequence = 0
        self._operation = ""
        self._state = "queued"
        self._step = "queued"
        self._completed: list[str] = []
        self._error = ""
        self._approval = "not_required"

    @workflow.signal
    def approve(self, approver: str) -> None:
        cleaned = approver.strip()
        self._approval = f"approved:{cleaned}" if cleaned else "approved"

    @workflow.signal
    def reject(self, approver: str) -> None:
        cleaned = approver.strip()
        self._approval = f"rejected:{cleaned}" if cleaned else "rejected"

    @workflow.query
    def status(self) -> dict[str, Any]:
        return {
            "operation": self._operation,
            "state": self._state,
            "step": self._step,
            "completed": list(self._completed),
            "error": self._error,
            "approval": self._approval,
        }

    @workflow.run
    async def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        workflow.patched(_PATCH)
        self._workflow_id = str(payload.get("workflow_id") or "")
        self._operation = str(payload.get("operation") or "")
        request = dict(payload.get("request") or {})
        principal = request.get("principal")
        self._principal = dict(principal) if isinstance(principal, dict) else {}
        self._state = "running"
        await self._transition(state="running", step="queued")
        try:
            if self._operation == "import":
                result = await self._activity(
                    "agent_import_verify",
                    dict(payload.get("request") or {}),
                    timeout=_SHORT_TIMEOUT,
                    step="verify_import",
                )
            elif self._operation == "provision":
                result = await self._activity(
                    "agent_sandbox_provision",
                    dict(payload.get("request") or {}),
                    timeout=_PROVISION_TIMEOUT,
                    step="provision_sandbox",
                    heartbeat=timedelta(seconds=30),
                )
            elif self._operation == "evaluate":
                result = await self._evaluate(payload)
            elif self._operation == "compare":
                result = await self._activity(
                    "agent_evaluation_compare",
                    dict(payload.get("request") or {}),
                    timeout=_SHORT_TIMEOUT,
                    step="compare_evaluation",
                )
            elif self._operation == "promote":
                result = await self._promote(payload)
            elif self._operation == "cleanup":
                result = await self._activity(
                    "agent_sandbox_cleanup",
                    dict(payload.get("request") or {}),
                    timeout=_CLEANUP_TIMEOUT,
                    step="cleanup_sandbox",
                )
            else:
                raise ValueError(f"unsupported agent lifecycle operation: {self._operation}")
        except asyncio.CancelledError:
            self._state = "cancelled"
            self._error = "cancelled"
            await self._transition(
                state=self._state,
                step=self._step,
                error_code="CancelledError",
                best_effort=True,
            )
            raise
        except TimeoutError as error:
            self._state = "timed_out"
            self._error = str(error)
            await self._transition(
                state=self._state,
                step=self._step,
                error_code=type(error).__name__,
                best_effort=True,
            )
            raise
        except Exception as error:
            if self._state != "stuck":
                self._state = "failed"
            self._error = str(error)
            await self._transition(
                state=self._state,
                step=self._step,
                error_code=type(error).__name__,
                best_effort=True,
            )
            raise
        self._state = "succeeded"
        self._step = "complete"
        await self._transition(state=self._state, step=self._step)
        return result

    async def _evaluate(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = dict(payload.get("request") or {})
        sandbox_id = str(request.get("sandbox_id") or "")
        provisioned = False
        primary_error: BaseException | None = None
        try:
            if not sandbox_id:
                sandbox = await self._activity(
                    "agent_sandbox_provision",
                    request,
                    timeout=_PROVISION_TIMEOUT,
                    step="provision_sandbox",
                    heartbeat=timedelta(seconds=30),
                )
                sandbox_id = str(sandbox.get("id") or "")
                if not sandbox_id:
                    raise RuntimeError("sandbox provision activity returned no id")
                provisioned = True
            evaluation_request = {**request, "sandbox_id": sandbox_id}
            result = await self._activity(
                "agent_evaluation_run",
                evaluation_request,
                timeout=_EVALUATION_TIMEOUT,
                step="run_evaluation",
                heartbeat=timedelta(seconds=30),
            )
            baseline = str(request.get("baseline_run_id") or "")
            if baseline:
                result["comparison"] = await self._activity(
                    "agent_evaluation_compare",
                    {"baseline_run_id": baseline, "candidate_run_id": result.get("id")},
                    timeout=_SHORT_TIMEOUT,
                    step="compare_evaluation",
                )
            return result
        except BaseException as error:
            primary_error = error
            raise
        finally:
            if provisioned and bool(payload.get("cleanup")):
                try:
                    await self._activity(
                        "agent_sandbox_cleanup",
                        {"sandbox_id": sandbox_id, "request": request},
                        timeout=_CLEANUP_TIMEOUT,
                        step="cleanup_sandbox",
                        heartbeat=timedelta(seconds=30),
                    )
                except BaseException:
                    if primary_error is None:
                        self._state = "stuck"
                        raise
                    workflow.logger.exception("sandbox cleanup failed after evaluation failure")
                    self._state = "stuck"

    async def _promote(self, payload: dict[str, Any]) -> dict[str, Any]:
        if bool(payload.get("requires_approval")):
            self._approval = self._approval if self._approval != "not_required" else "pending"
            self._step = "await_approval"
            await workflow.wait_condition(
                lambda: self._approval.startswith("approved") or self._approval.startswith("rejected")
            )
            if self._approval.startswith("rejected"):
                raise ValueError("promotion rejected")
        return await self._activity(
            "agent_promote",
            dict(payload.get("request") or {}),
            timeout=_PROVISION_TIMEOUT,
            step="promote_agent",
        )

    async def _activity(
        self,
        name: str,
        request: dict[str, Any],
        *,
        timeout: timedelta,
        step: str,
        heartbeat: timedelta | None = None,
    ) -> dict[str, Any]:
        self._step = step
        result = await workflow.execute_activity(
            name,
            args=[request],
            start_to_close_timeout=timeout,
            heartbeat_timeout=heartbeat,
            retry_policy=_retry_policy(),
        )
        self._completed.append(step)
        await self._transition(state="running", step=step)
        return dict(result)

    async def _transition(
        self,
        *,
        state: str,
        step: str,
        error_code: str = "",
        best_effort: bool = False,
    ) -> None:
        if not self._workflow_id or not self._principal:
            return
        self._transition_sequence += 1
        payload = {
            "principal": self._principal,
            "workflow_id": self._workflow_id,
            "sequence": self._transition_sequence,
            "operation": self._operation,
            "state": state,
            "step": step,
            "error_code": error_code,
        }
        try:
            await workflow.execute_activity(
                "agent_lifecycle_transition",
                args=[payload],
                start_to_close_timeout=_SHORT_TIMEOUT,
                retry_policy=_retry_policy(),
            )
        except BaseException:
            if not best_effort:
                raise
            workflow.logger.exception("agent lifecycle terminal transition could not be recorded")


__all__ = ["AgentLifecycleWorkflow"]
