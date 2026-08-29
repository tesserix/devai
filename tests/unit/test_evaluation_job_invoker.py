from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from devai.config import Settings
from devai.evaluations.job import JobEvaluationInvoker
from devai.pipeline.interfaces import StageDeps
from devai.pipeline.types import StageResult
from devai.sandbox.models import AgentRef, ModelRef, SandboxRecord, SandboxSpec, SandboxStatus
from devai.sandbox.trace import TraceStore


def _record() -> SandboxRecord:
    now = datetime.now(UTC)
    return SandboxRecord(
        id="sb-1",
        owner="tenant-a:alice",
        spec=SandboxSpec(
            agent=AgentRef(name="support-agent", version="7"),
            model=ModelRef(provider="anthropic", model="claude-sonnet-4"),
        ),
        status=SandboxStatus.READY,
        created_at=now,
        expires_at=now + timedelta(hours=1),
    )


class _Stage:
    def __init__(self, result: StageResult) -> None:
        self.result = result
        self.tasks: list[Any] = []

    async def execute(self, task: Any) -> StageResult:
        self.tasks.append(task)
        return self.result


class _Factory:
    def __init__(self, stage: _Stage) -> None:
        self.stage = stage
        self.configs: list[dict[str, Any]] = []

    def __call__(self, _deps: StageDeps, config: dict[str, Any]) -> _Stage:
        self.configs.append(config)
        return self.stage


async def test_evaluation_case_dispatches_through_the_sandboxed_job_stage_and_persists_a_trace() -> None:
    stage = _Stage(
        StageResult(
            message="job complete",
            data={
                "evaluation_output": {
                    "ok": True,
                    "support_agent_text": "refund complete",
                    "usage": {
                        "prompt_tokens": 80,
                        "completion_tokens": 20,
                        "latency_ms": 125,
                    },
                    "trace_steps": [
                        {
                            "kind": "tool",
                            "name": "refund",
                            "input": {"order_id": "4471"},
                            "output": "refunded",
                            "mode": "mock",
                            "latency_ms": 4,
                        }
                    ],
                }
            },
        )
    )
    factory = _Factory(stage)
    traces = TraceStore(None)
    deps = StageDeps(config=Settings(), extra={"k8s_runtime": object(), "job_watcher": object()})
    invoker = JobEvaluationInvoker(deps=deps, traces=traces, fallback=None, stage_factory=factory)

    invocation = await invoker.invoke(_record(), message="refund order 4471", triggered_by="tenant-a:alice")

    assert invoker.execution_backend == "kubernetes_job"
    assert factory.configs[0]["agent"] == "support-agent"
    assert factory.configs[0]["__sandbox"]["id"] == "sb-1"
    assert stage.tasks[0].intent == "refund order 4471"
    assert invocation.ok
    assert invocation.execution_backend == "kubernetes_job"
    assert invocation.final_text == "refund complete"
    assert invocation.totals["total_tokens"] == 100
    assert invocation.totals["cost_usd"] == 0.00054
    assert [(step.kind, step.name, step.mode) for step in invocation.steps] == [
        ("prompt", "user", ""),
        ("tool", "refund", "mock"),
        ("llm", "claude-sonnet-4", ""),
        ("response", "final", ""),
    ]
    assert await traces.get("sb-1", invocation.id) is not None
    persisted = await traces.get("sb-1", invocation.id)
    assert persisted is not None
    assert persisted.execution_backend == "kubernetes_job"


async def test_draft_sandbox_bypasses_the_job_stage_for_the_inline_invoker() -> None:
    class _Fallback:
        async def invoke(self, record: SandboxRecord, *, message: str, triggered_by: str):
            del message, triggered_by
            from devai.sandbox.trace import Invocation

            return Invocation(id="inline-draft", sandbox_id=record.id, agent="measure-mate")

    record = _record()
    record = record.model_copy(
        update={"spec": record.spec.model_copy(update={"draft": {"metadata": {"name": "measure-mate"}, "spec": {}}})}
    )
    invoker = JobEvaluationInvoker(
        deps=StageDeps(config=Settings(), extra={"k8s_runtime": object(), "job_watcher": object()}),
        traces=TraceStore(None),
        fallback=_Fallback(),  # type: ignore[arg-type]
    )

    invocation = await invoker.invoke(record, message="go", triggered_by="tenant-a:alice")

    assert invoker.execution_backend == "kubernetes_job"
    assert invocation.id == "inline-draft"
    assert invocation.execution_backend == "inline"


async def test_missing_job_runtime_uses_the_explicit_local_fallback() -> None:
    class _Fallback:
        async def invoke(self, record: SandboxRecord, *, message: str, triggered_by: str):
            del message, triggered_by
            from devai.sandbox.trace import Invocation

            return Invocation(id="inline-1", sandbox_id=record.id, agent=record.spec.agent.name)

    invoker = JobEvaluationInvoker(
        deps=StageDeps(config=Settings(), extra={}),
        traces=TraceStore(None),
        fallback=_Fallback(),  # type: ignore[arg-type]
    )

    invocation = await invoker.invoke(_record(), message="go", triggered_by="tenant-a:alice")

    assert invoker.execution_backend == "inline"
    assert invocation.id == "inline-1"
    assert invocation.execution_backend == "inline"
