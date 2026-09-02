from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol

from devai.pipeline.interfaces import StageDeps
from devai.pipeline.types import DevAITask, StageResult
from devai.sandbox.models import SandboxRecord
from devai.sandbox.trace import Invocation, TraceStep, TraceStore

logger = logging.getLogger(__name__)


class EvaluationInvoker(Protocol):
    async def invoke(self, record: SandboxRecord, *, message: str, triggered_by: str) -> Invocation: ...


class EvaluationStage(Protocol):
    async def execute(self, task: DevAITask) -> StageResult: ...


StageFactory = Callable[[StageDeps, dict[str, Any]], EvaluationStage]


class JobEvaluationInvoker:
    def __init__(
        self,
        *,
        deps: StageDeps,
        traces: TraceStore,
        fallback: EvaluationInvoker | None,
        stage_factory: StageFactory | None = None,
    ) -> None:
        if stage_factory is None:
            from devai.pipeline.stages.job_runner import JobRunnerStage

            stage_factory = JobRunnerStage
        self._deps = deps
        self._traces = traces
        self._fallback = fallback
        self._stage_factory = stage_factory

    @property
    def execution_backend(self) -> str:
        return "kubernetes_job" if self._job_enabled() else "inline"

    async def invoke(self, record: SandboxRecord, *, message: str, triggered_by: str) -> Invocation:
        # A draft envelope has no Registry record yet, so governed Job dispatch
        # would fail closed; the inline invoker is the draft-aware path.
        if not self._job_enabled() or record.spec.draft:
            if self._fallback is None:
                raise RuntimeError("evaluation Job runtime unavailable")
            invocation = await self._fallback.invoke(record, message=message, triggered_by=triggered_by)
            invocation.execution_backend = "inline"
            return invocation

        invocation = Invocation(
            id=f"inv-{uuid.uuid4().hex[:12]}",
            sandbox_id=record.id,
            agent=record.spec.agent.name,
            message=message,
            execution_backend="kubernetes_job",
            steps=[TraceStep(kind="prompt", name="user", output=message)],
        )
        started = time.perf_counter()
        try:
            result = await self._run_job(record, invocation, message=message, triggered_by=triggered_by)
            output = result.data.get("evaluation_output") or {}
            if not isinstance(output, dict):
                output = {"value": output}
            invocation.ok = bool(output.get("ok", True))
            invocation.final_text = self._final_text(output, result)
            invocation.error = str(output.get("error") or "")[:500]
            invocation.steps.extend(self._result_steps(record, output, invocation.final_text))
        except Exception as error:  # noqa: BLE001 — a failed Job is a failed case result
            logger.warning("sandbox %s: evaluation Job failed", record.id, exc_info=True)
            invocation.ok = False
            invocation.error = str(error)[:500]
            invocation.steps.append(
                TraceStep(
                    kind="response",
                    name="job_failure",
                    error=invocation.error,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                )
            )
        invocation.wall_clock_ms = max(1, int((time.perf_counter() - started) * 1000))
        await self._traces.save(invocation, ttl_seconds=self._ttl(record))
        return invocation

    async def _run_job(
        self,
        record: SandboxRecord,
        invocation: Invocation,
        *,
        message: str,
        triggered_by: str,
    ) -> StageResult:
        config: dict[str, Any] = {
            "agent": record.spec.agent.name,
            "agent_version": record.spec.agent.version,
            "model_provider": record.spec.model.provider,
            "model_name": record.spec.model.model,
            "__stage_name": "evaluation",
            "__sandbox": record.model_dump(mode="json"),
        }
        stage = self._stage_factory(self._deps, config)
        task = DevAITask(
            id=f"eval-case-{uuid.uuid4().hex[:12]}",
            intent=message,
            blueprint="evaluation",
            triggered_by=triggered_by,
            trace_id=invocation.id,
        )
        return await stage.execute(task)

    def _job_enabled(self) -> bool:
        extra = self._deps.extra or {}
        return extra.get("k8s_runtime") is not None and extra.get("job_watcher") is not None

    @staticmethod
    def _final_text(output: dict[str, Any], result: StageResult) -> str:
        for key in ("final_text", "message", "output", "value"):
            value = output.get(key)
            if isinstance(value, str):
                return value
        for key, value in output.items():
            if key.endswith("_text") and isinstance(value, str):
                return value
        return result.message

    @staticmethod
    def _result_steps(record: SandboxRecord, output: dict[str, Any], final_text: str) -> list[TraceStep]:
        usage = output.get("usage") or {}
        if not isinstance(usage, dict):
            usage = {}
        steps: list[TraceStep] = []
        for raw in output.get("trace_steps") or []:
            if not isinstance(raw, dict) or raw.get("kind") != "tool":
                continue
            steps.append(
                TraceStep(
                    kind="tool",
                    name=str(raw.get("name") or ""),
                    input=raw.get("input"),
                    output=raw.get("output"),
                    mode=str(raw.get("mode") or ""),
                    latency_ms=int(raw.get("latency_ms") or 0),
                    error=str(raw.get("error") or "")[:500],
                )
            )
        if usage:
            cost_usd = float(usage.get("cost_usd") or 0.0)
            if not cost_usd:
                from devai.analytics.pricing import estimate_cost

                cost_usd = estimate_cost(
                    record.spec.model.provider,
                    record.spec.model.model,
                    int(usage.get("prompt_tokens") or 0),
                    int(usage.get("completion_tokens") or 0),
                )
            steps.append(
                TraceStep(
                    kind="llm",
                    name=record.spec.model.model,
                    provider=record.spec.model.provider,
                    prompt_version=(
                        record.spec.prompt.version if record.spec.prompt is not None else record.spec.agent.version
                    ),
                    prompt_tokens=int(usage.get("prompt_tokens") or 0),
                    completion_tokens=int(usage.get("completion_tokens") or 0),
                    cost_usd=cost_usd,
                    latency_ms=int(usage.get("latency_ms") or 0),
                )
            )
        steps.append(TraceStep(kind="response", name="final", output=final_text))
        return steps

    @staticmethod
    def _ttl(record: SandboxRecord) -> int:
        return max(int((record.expires_at - datetime.now(UTC)).total_seconds()), 300)


__all__ = ["EvaluationInvoker", "JobEvaluationInvoker"]
