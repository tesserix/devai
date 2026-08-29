from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from temporalio.exceptions import ApplicationError

from devai.evaluations.models import ArtifactVersionRef
from devai.identity import Principal
from devai.orchestration.agent_lifecycle_activities import AgentLifecycleActivities
from devai.registry.promotion import AgentPromotionError
from devai.sandbox.models import SandboxRecord, SandboxSpec, SandboxStatus


class _Imports:
    async def create(self, principal: Principal, **values: str) -> dict[str, Any]:
        return {"tenant": principal.tenant_id, **values, "state": "ready"}


class _Sandboxes:
    destroyed: tuple[str, str] | None = None

    async def destroy(self, sandbox_id: str, *, owner: str, is_admin: bool) -> None:
        assert is_admin is False
        self.destroyed = (sandbox_id, owner)


@pytest.mark.asyncio
async def test_import_activity_reconstructs_only_the_server_supplied_principal() -> None:
    activities = AgentLifecycleActivities(
        imports=_Imports(),
        sandboxes=_Sandboxes(),
        evaluations=SimpleNamespace(),
        runner=SimpleNamespace(),
    )

    result = await activities.verify_import(
        {
            "principal": Principal(email="alice@example.com", uid="alice", tenant_id="acme").to_dict(),
            "project_id": "support-lab",
            "registry_ref": "registry://acme/agents/acme/support@1.4.0",
            "idempotency_key": "import-1",
        }
    )

    assert result["tenant"] == "acme"
    assert result["project_id"] == "support-lab"


@pytest.mark.asyncio
async def test_cleanup_activity_scopes_the_delete_to_the_workflow_principal() -> None:
    sandboxes = _Sandboxes()
    activities = AgentLifecycleActivities(
        imports=_Imports(),
        sandboxes=sandboxes,
        evaluations=SimpleNamespace(),
        runner=SimpleNamespace(),
    )

    result = await activities.cleanup_sandbox(
        {
            "principal": Principal(email="alice@example.com", uid="alice", tenant_id="acme").to_dict(),
            "sandbox_id": "sandbox-1",
        }
    )

    assert result == {"destroyed": "sandbox-1"}
    assert sandboxes.destroyed == ("sandbox-1", "acme:alice")


@pytest.mark.asyncio
async def test_activity_validation_errors_are_non_retryable() -> None:
    activities = AgentLifecycleActivities(
        imports=_Imports(),
        sandboxes=_Sandboxes(),
        evaluations=SimpleNamespace(),
        runner=SimpleNamespace(),
    )

    with pytest.raises(ApplicationError) as caught:
        await activities.verify_import({"principal": {}})

    assert caught.value.type == "AgentLifecycleValidationError"
    assert caught.value.non_retryable is True


@pytest.mark.asyncio
async def test_provision_activity_derives_one_sandbox_id_from_the_idempotency_key() -> None:
    class _CreatingSandboxes:
        ids: list[str | None] = []

        async def create(
            self, spec: SandboxSpec, *, owner: str, sandbox_id: str | None = None, **_: str
        ) -> SandboxRecord:
            self.ids.append(sandbox_id)
            now = datetime.now(UTC)
            return SandboxRecord(
                id=sandbox_id or "random",
                owner=owner,
                spec=spec,
                status=SandboxStatus.READY,
                created_at=now,
                expires_at=now + timedelta(hours=1),
            )

    sandboxes = _CreatingSandboxes()
    activities = AgentLifecycleActivities(
        imports=_Imports(),
        sandboxes=sandboxes,
        evaluations=SimpleNamespace(),
        runner=SimpleNamespace(),
    )
    payload = {
        "principal": Principal(email="alice@example.com", uid="alice", tenant_id="acme").to_dict(),
        "idempotency_key": "sandbox-request-42",
        "sandbox": {
            "agent": {"name": "support", "version": "1.4.0"},
            "model": {"provider": "portable", "model": "external"},
        },
    }

    first = await activities.provision_sandbox(payload)
    second = await activities.provision_sandbox(payload)

    assert first["id"] == second["id"]
    assert first["id"].startswith("sbx-")
    assert sandboxes.ids == [first["id"], first["id"]]


@pytest.mark.asyncio
async def test_evaluation_provision_pins_the_suites_exact_dataset_before_creation() -> None:
    class _CreatingSandboxes:
        spec: SandboxSpec | None = None

        async def create(
            self, spec: SandboxSpec, *, owner: str, sandbox_id: str | None = None, **_: str
        ) -> SandboxRecord:
            self.spec = spec
            now = datetime.now(UTC)
            return SandboxRecord(
                id=sandbox_id or "random",
                owner=owner,
                spec=spec,
                status=SandboxStatus.READY,
                created_at=now,
                expires_at=now + timedelta(hours=1),
            )

    class _Evaluations:
        async def resolve_suite(self, principal: Principal, requested: ArtifactVersionRef) -> Any:
            assert principal.tenant_id == "acme"
            assert requested == ArtifactVersionRef(name="release-gate", version="2")
            return SimpleNamespace(dataset=ArtifactVersionRef(name="golden", version="3"))

    sandboxes = _CreatingSandboxes()
    activities = AgentLifecycleActivities(
        imports=_Imports(),
        sandboxes=sandboxes,
        evaluations=_Evaluations(),
        runner=SimpleNamespace(),
    )

    await activities.provision_sandbox(
        {
            "principal": Principal(email="alice@example.com", uid="alice", tenant_id="acme").to_dict(),
            "idempotency_key": "evaluation-request-42",
            "idempotency_scope": "evaluation",
            "suite": {"name": "release-gate", "version": "2"},
            "sandbox": {
                "agent": {"name": "support", "version": "1.4.0"},
                "model": {"provider": "portable", "model": "external"},
            },
        }
    )

    assert sandboxes.spec is not None
    assert sandboxes.spec.dataset is not None
    assert sandboxes.spec.dataset.ref == "golden"
    assert sandboxes.spec.dataset.version == "3"


@pytest.mark.asyncio
async def test_real_tool_evaluation_failure_is_non_retryable_after_execution_starts() -> None:
    now = datetime.now(UTC)
    record = SandboxRecord(
        id="sandbox-1",
        owner="acme:alice",
        spec=SandboxSpec.model_validate(
            {
                "agent": {"name": "support", "version": "1.4.0"},
                "model": {"provider": "portable", "model": "external"},
                "tools": {"default_mode": "real"},
            }
        ),
        status=SandboxStatus.READY,
        created_at=now,
        expires_at=now + timedelta(hours=1),
    )

    class _EvaluationSandboxes:
        async def get(self, sandbox_id: str, *, owner: str, is_admin: bool) -> SandboxRecord:
            assert (sandbox_id, owner, is_admin) == ("sandbox-1", "acme:alice", False)
            return record

    class _Runner:
        async def run(self, *_: Any, **__: Any) -> Any:
            raise ConnectionError("result store unavailable after invocation")

    activities = AgentLifecycleActivities(
        imports=_Imports(),
        sandboxes=_EvaluationSandboxes(),
        evaluations=SimpleNamespace(),
        runner=_Runner(),
    )

    with pytest.raises(ApplicationError) as caught:
        await activities.run_evaluation(
            {
                "principal": Principal(email="alice@example.com", uid="alice", tenant_id="acme").to_dict(),
                "sandbox_id": "sandbox-1",
                "idempotency_key": "evaluation-request-42",
                "cases": [{"name": "one", "input": "hello"}],
            }
        )

    assert caught.value.type == "AgentLifecycleSideEffectError"
    assert caught.value.non_retryable is True


@pytest.mark.asyncio
async def test_promotion_policy_failures_are_non_retryable_but_dependency_failures_retry() -> None:
    principal = Principal(email="alice@example.com", uid="alice", tenant_id="acme")

    async def blocked(_principal: Principal, _payload: dict[str, Any]) -> dict[str, Any]:
        raise AgentPromotionError(422, {"code": "agent_evaluation_gate_blocked"})

    blocked_activities = AgentLifecycleActivities(
        imports=_Imports(),
        sandboxes=_Sandboxes(),
        evaluations=SimpleNamespace(),
        runner=SimpleNamespace(),
        promote=blocked,
    )
    with pytest.raises(ApplicationError) as caught:
        await blocked_activities.promote_agent({"principal": principal.to_dict(), "manifest": {}})
    assert caught.value.type == "AgentLifecyclePolicyError"
    assert caught.value.non_retryable is True

    async def unavailable(_principal: Principal, _payload: dict[str, Any]) -> dict[str, Any]:
        raise AgentPromotionError(503, "registry unavailable")

    retryable_activities = AgentLifecycleActivities(
        imports=_Imports(),
        sandboxes=_Sandboxes(),
        evaluations=SimpleNamespace(),
        runner=SimpleNamespace(),
        promote=unavailable,
    )
    with pytest.raises(AgentPromotionError) as retryable:
        await retryable_activities.promote_agent({"principal": principal.to_dict(), "manifest": {}})
    assert retryable.value.status_code == 503


@pytest.mark.asyncio
async def test_transition_activity_writes_only_tenant_scoped_state_metadata() -> None:
    class _Events:
        values: dict[str, Any] | None = None

        async def record_agent_lifecycle_event(self, **values: Any) -> dict[str, Any]:
            self.values = values
            return values

    events = _Events()
    activities = AgentLifecycleActivities(
        imports=_Imports(),
        sandboxes=_Sandboxes(),
        evaluations=SimpleNamespace(),
        runner=SimpleNamespace(),
        events=events,
    )
    principal = Principal(email="alice@example.com", uid="alice", tenant_id="acme")

    result = await activities.record_transition(
        {
            "principal": principal.to_dict(),
            "workflow_id": "agent-eval:acme:agent-lab:digest",
            "sequence": 3,
            "operation": "evaluate",
            "state": "running",
            "step": "run_evaluation",
            "error_code": "",
            "manifest": {"secret": "must-not-be-recorded"},
        }
    )

    assert result["workflow_id"] == "agent-eval:acme:agent-lab:digest"
    assert events.values is not None
    assert events.values["owner_scope"] == "acme:alice"
    assert events.values["tenant_id"] == "acme"
    assert "manifest" not in events.values
    assert "secret" not in str(events.values)
