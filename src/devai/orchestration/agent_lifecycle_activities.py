"""Temporal activities backed by the existing Agent import and lab services."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from devai.identity import Principal
from devai.registry.imports import AgentImportInvalid, public_agent_import
from devai.sandbox.models import DatasetRef, SandboxSpec, SandboxStatus, ToolMode


class AgentLifecycleActivities:
    """Bind workflow activity names to tenant-scoped domain services."""

    def __init__(
        self,
        *,
        imports: Any,
        sandboxes: Any,
        evaluations: Any,
        runner: Any,
        promote: Callable[[Principal, dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
        events: Any | None = None,
    ) -> None:
        self._imports = imports
        self._sandboxes = sandboxes
        self._evaluations = evaluations
        self._runner = runner
        self._promote = promote
        self._events = events

    @activity.defn(name="agent_import_verify")
    async def verify_import(self, payload: dict[str, Any]) -> dict[str, Any]:
        principal = _principal(payload)
        try:
            result = await self._imports.create(
                principal,
                project_id=_required(payload, "project_id"),
                registry_ref=_required(payload, "registry_ref"),
                idempotency_key=_required(payload, "idempotency_key"),
            )
        except AgentImportInvalid as error:
            raise _validation(error) from error
        return public_agent_import(result)

    @activity.defn(name="agent_sandbox_provision")
    async def provision_sandbox(self, payload: dict[str, Any]) -> dict[str, Any]:
        from devai.evaluations.models import ArtifactVersionRef
        from devai.evaluations.service import EvaluationNotFound

        principal = _principal(payload)
        raw = payload.get("sandbox") or payload.get("spec")
        if not isinstance(raw, dict):
            raise _validation(ValueError("sandbox specification is required"))
        try:
            spec = SandboxSpec.model_validate(raw)
            if isinstance(payload.get("suite"), dict):
                requested = ArtifactVersionRef.model_validate(payload["suite"])
                resolved = await self._evaluations.resolve_suite(principal, requested)
                pinned_dataset = DatasetRef(ref=resolved.dataset.name, version=resolved.dataset.version)
                if spec.dataset is not None and spec.dataset != pinned_dataset:
                    raise ValueError("sandbox dataset does not match the suite's pinned dataset version")
                spec = spec.model_copy(update={"dataset": pinned_dataset})
            sandbox_id = _stable_resource_id("sbx", principal, payload)
            record = await self._sandboxes.create(
                spec,
                owner=_owner(principal),
                tenant_id=principal.tenant_id,
                user_id=principal.uid or principal.email,
                sandbox_id=sandbox_id,
            )
        except (EvaluationNotFound, ValueError) as error:
            raise _validation(error) from error
        return _object_result(record.model_dump(mode="json"))

    @activity.defn(name="agent_evaluation_run")
    async def run_evaluation(self, payload: dict[str, Any]) -> dict[str, Any]:
        from devai.evaluations.models import ArtifactVersionRef
        from devai.sandbox.evals import EvalCase

        principal = _principal(payload)
        owner = _owner(principal)
        sandbox_id = _required(payload, "sandbox_id")
        record = await self._sandboxes.get(sandbox_id, owner=owner, is_admin=False)
        if record is None:
            raise _validation(ValueError(f"sandbox {sandbox_id} not found"))
        if record.status != SandboxStatus.READY:
            raise _validation(ValueError(f"sandbox {sandbox_id} is not ready"))

        dataset_ref: dict[str, Any] | None = None
        suite_ref: dict[str, Any] | None = None
        scorers: list[str] = []
        judge_config = None
        try:
            if isinstance(payload.get("suite"), dict):
                requested = ArtifactVersionRef.model_validate(payload["suite"])
                resolved = await self._evaluations.resolve_suite(principal, requested)
                cases = resolved.cases
                dataset_ref = resolved.dataset.model_dump(mode="json")
                suite_ref = (
                    resolved.suite.model_dump(mode="json") if resolved.suite else requested.model_dump(mode="json")
                )
                scorers = resolved.scorers
                judge_config = resolved.judge
                if record.spec.dataset != DatasetRef(ref=resolved.dataset.name, version=resolved.dataset.version):
                    raise ValueError("sandbox does not pin the suite's exact dataset version")
            elif isinstance(payload.get("dataset"), dict):
                requested = ArtifactVersionRef.model_validate(payload["dataset"])
                resolved = await self._evaluations.resolve_dataset(principal, requested)
                cases = resolved.cases
                dataset_ref = resolved.dataset.model_dump(mode="json")
            elif isinstance(payload.get("cases"), list):
                cases = [EvalCase.model_validate(item) for item in payload["cases"]]
            else:
                raise ValueError("evaluation needs exactly one suite, dataset, or cases source")
        except ValueError as error:
            raise _validation(error) from error

        try:
            run = await _with_heartbeat(
                self._runner.run(
                    record,
                    cases,
                    triggered_by=owner,
                    owner_scope=owner,
                    tenant_id=principal.tenant_id,
                    user_id=principal.uid or principal.email,
                    dataset_ref=dataset_ref,
                    suite_ref=suite_ref,
                    scorers=scorers,
                    principal=principal,
                    judge_config=judge_config,
                    run_id=_stable_resource_id("eval", principal, payload),
                ),
                f"evaluate:{sandbox_id}",
            )
        except ValueError as error:
            raise _validation(error) from error
        except Exception as error:
            if _may_execute_real_tools(record.spec):
                raise ApplicationError(
                    "real-tool evaluation failed after execution began; automatic retry is unsafe",
                    type="AgentLifecycleSideEffectError",
                    non_retryable=True,
                ) from error
            raise
        return _object_result(run.to_dict())

    @activity.defn(name="agent_evaluation_compare")
    async def compare_evaluation(self, payload: dict[str, Any]) -> dict[str, Any]:
        from devai.evaluations.models import ComparisonCreate

        principal = _principal(payload)
        try:
            request = ComparisonCreate(
                baseline_run_id=_required(payload, "baseline_run_id"),
                candidate_run_id=_required(payload, "candidate_run_id"),
            )
            comparison = await self._evaluations.create_comparison(principal, request)
        except ValueError as error:
            raise _validation(error) from error
        return _object_result(comparison.model_dump(mode="json"))

    @activity.defn(name="agent_sandbox_cleanup")
    async def cleanup_sandbox(self, payload: dict[str, Any]) -> dict[str, Any]:
        principal = _principal(payload)
        sandbox_id = _required(payload, "sandbox_id")
        await _with_heartbeat(
            self._sandboxes.destroy(sandbox_id, owner=_owner(principal), is_admin=False),
            f"cleanup:{sandbox_id}",
        )
        return {"destroyed": sandbox_id}

    @activity.defn(name="agent_promote")
    async def promote_agent(self, payload: dict[str, Any]) -> dict[str, Any]:
        from devai.registry.promotion import AgentPromotionError

        if self._promote is None:
            raise RuntimeError("agent promotion activity is not configured")
        try:
            return await self._promote(_principal(payload), payload)
        except AgentPromotionError as error:
            if error.status_code < 500:
                raise ApplicationError(
                    str(error.detail), type="AgentLifecyclePolicyError", non_retryable=True
                ) from error
            raise

    @activity.defn(name="agent_lifecycle_transition")
    async def record_transition(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._events is None:
            raise RuntimeError("agent lifecycle event storage is not configured")
        principal = _principal(payload)
        workflow_id = _required(payload, "workflow_id")
        operation = _required(payload, "operation")
        state = _required(payload, "state")
        step = _required(payload, "step")
        sequence = payload.get("sequence")
        if type(sequence) is not int or sequence < 1:
            raise _validation(ValueError("positive lifecycle transition sequence is required"))
        event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"devai:lifecycle:{workflow_id}:{sequence}"))
        return _object_result(
            await self._events.record_agent_lifecycle_event(
                id=event_id,
                workflow_id=workflow_id,
                sequence=sequence,
                owner_scope=_owner(principal),
                tenant_id=principal.tenant_id,
                operation=operation,
                state=state,
                step=step,
                error_code=str(payload.get("error_code") or "")[:200],
                created_at=datetime.now(UTC),
            )
        )

    def registered(self) -> list[Callable[..., Awaitable[dict[str, Any]]]]:
        return [
            self.verify_import,
            self.provision_sandbox,
            self.run_evaluation,
            self.compare_evaluation,
            self.cleanup_sandbox,
            self.promote_agent,
            self.record_transition,
        ]


def _principal(payload: dict[str, Any]) -> Principal:
    raw = payload.get("principal")
    principal = Principal.from_dict(raw if isinstance(raw, dict) else None)
    if principal is None or not principal.user_scope_id:
        raise _validation(ValueError("verified workflow principal is required"))
    return principal


def _object_result(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise RuntimeError("agent lifecycle service returned a non-object result")
    return value


def _owner(principal: Principal) -> str:
    owner = principal.user_scope_id
    if not owner:
        raise _validation(ValueError("verified workflow principal has no stable subject"))
    return owner


def _required(payload: dict[str, Any], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise _validation(ValueError(f"{name} is required"))
    return value.strip()


def _stable_resource_id(prefix: str, principal: Principal, payload: dict[str, Any]) -> str:
    key = _required(payload, "idempotency_key")
    scope = str(payload.get("idempotency_scope") or "lifecycle")
    canonical = "\x00".join((principal.tenant_id, principal.user_scope_id, scope, key))
    return f"{prefix}-{hashlib.sha256(canonical.encode()).hexdigest()[:32]}"


def _may_execute_real_tools(spec: SandboxSpec) -> bool:
    return spec.tools.default_mode == ToolMode.REAL or ToolMode.REAL in spec.tools.overrides.values()


def _validation(error: BaseException) -> ApplicationError:
    return ApplicationError(str(error), type="AgentLifecycleValidationError", non_retryable=True)


async def _with_heartbeat[T](awaitable: Awaitable[T], label: str) -> T:
    complete = asyncio.Event()

    async def beat() -> None:
        while not complete.is_set():
            with contextlib.suppress(RuntimeError):
                activity.heartbeat({"operation": label})
            try:
                await asyncio.wait_for(complete.wait(), timeout=10)
            except TimeoutError:
                continue

    heartbeat = asyncio.create_task(beat())
    try:
        return await awaitable
    finally:
        complete.set()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat


__all__ = ["AgentLifecycleActivities"]
