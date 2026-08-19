"""Run one turn of a pinned agent inside its sandbox and record what happened.

The sandbox already pins *what* runs (agent, model, prompt, tools, limits). This
is the part that runs it: it resolves the pinned agent to a Specialization,
forces the pinned model over whatever the role would otherwise choose, routes
every tool call through the sandbox gateway, and writes the trace.

Nothing here re-implements the agent loop — `SpecAgent`/`AgentRunner` is the one
loop, the same one a pipeline stage or a dispatched Job takes. A sandbox differs
only in its boundaries, which is the whole point: what you test is what ships.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from devai.sandbox.gateway import ToolCallRecord, ToolGateway
from devai.sandbox.models import SandboxRecord, SandboxStatus
from devai.sandbox.trace import Invocation, TraceStep, TraceStore

if TYPE_CHECKING:
    from devai.pipeline.interfaces import StageDeps
    from devai.sandbox.credentials import SandboxCredentialProvider
    from devai.specializations.base import Specialization
    from devai.specializations.service import SpecializationService

logger = logging.getLogger(__name__)

_LIVE = {SandboxStatus.PENDING, SandboxStatus.PROVISIONING, SandboxStatus.READY}
_MIN_TTL_SECONDS = 300


class SandboxInvoker:
    def __init__(
        self,
        *,
        specializations: SpecializationService,
        deps: StageDeps,
        traces: TraceStore,
        credentials: SandboxCredentialProvider | None = None,
    ) -> None:
        from devai.sandbox.credentials import SandboxCredentialResolver

        self._specs = specializations
        self._deps = deps
        self._traces = traces
        self._credentials = credentials or SandboxCredentialResolver()

    async def invoke(self, record: SandboxRecord, *, message: str, triggered_by: str) -> Invocation:
        """One turn. Model and tool failures become a failed trace, never a lost one."""
        if record.status not in _LIVE:
            raise ValueError(f"sandbox {record.id} is {record.status.value} and cannot be invoked")
        spec = await self._resolve(record)

        steps: list[TraceStep] = [
            TraceStep(kind="prompt", name="system", output=spec.system_prompt),
            TraceStep(kind="prompt", name="user", output=message),
        ]
        gateway = ToolGateway(policy=record.spec.tools, sink=lambda rec: steps.append(_tool_step(rec)))

        invocation = Invocation(
            id=f"inv-{uuid.uuid4().hex[:12]}",
            sandbox_id=record.id,
            agent=spec.name,
            message=message,
            steps=steps,
        )
        started = time.perf_counter()
        try:
            try:
                async with asyncio.timeout(record.spec.limits.max_wall_clock_s):
                    result = await self._run(spec, record, gateway, message=message, triggered_by=triggered_by)
            except TimeoutError as exc:
                from devai.sandbox.credentials import SandboxBudgetExceeded

                raise SandboxBudgetExceeded(
                    f"sandbox {record.id} exceeded its wall-clock budget ({record.spec.limits.max_wall_clock_s}s)"
                ) from exc
            invocation.final_text = result.final_text
            invocation.ok = result.ok
            invocation.error = result.error or ""
            from devai.analytics.pricing import estimate_cost

            steps.append(
                TraceStep(
                    kind="llm",
                    name=record.spec.model.model,
                    prompt_tokens=result.prompt_tokens,
                    completion_tokens=result.completion_tokens,
                    cost_usd=estimate_cost(
                        record.spec.model.provider,
                        record.spec.model.model,
                        result.prompt_tokens,
                        result.completion_tokens,
                    ),
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    error=result.error or "",
                )
            )
            steps.append(TraceStep(kind="response", name="final", output=result.final_text))
        except Exception as exc:  # noqa: BLE001 — a broken run is evidence, not an outage
            from devai.services.redact import redact_secrets

            safe_error = redact_secrets(str(exc))[:500]
            logger.warning("sandbox %s: invocation failed: %s", record.id, safe_error)
            invocation.ok = False
            invocation.error = safe_error
            steps.append(
                TraceStep(
                    kind="llm",
                    name=record.spec.model.model,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    error=safe_error,
                )
            )

        await self._traces.save(invocation, ttl_seconds=self._ttl(record))
        return invocation

    async def _resolve(self, record: SandboxRecord) -> Specialization:
        from devai.registry.mapping import agent_envelope_to_spec, role_name

        spec: Specialization | None
        if record.spec.draft:
            spec = agent_envelope_to_spec(record.spec.draft)
        else:
            spec = await self._specs.resolve_runnable(role_name(record.spec.agent.name))
        if spec is None:
            raise ValueError(f"sandbox {record.id} pins agent {record.spec.agent.name!r}, which is not runnable here")
        # The sandbox's pin beats the role's own preference — that is what makes
        # a result attributable to a configuration rather than to a default.
        return replace(
            spec,
            llm_model=record.spec.model.model or spec.llm_model,
            max_turns=min(spec.max_turns, 8),
        )

    async def _run(
        self,
        spec: Specialization,
        record: SandboxRecord,
        gateway: ToolGateway,
        *,
        message: str,
        triggered_by: str,
    ) -> Any:
        from devai.agentruntime import AgentDispatcher, SpecAgent
        from devai.pipeline.types import DevAITask

        deps = await self._credentials.resolve(record, self._deps)
        deps = self._with_gateway(deps, record, gateway, triggered_by=triggered_by)
        task = DevAITask(intent=message, triggered_by=f"sandbox:{record.id}")
        # The synthetic principal prevents the normal dispatcher from resolving
        # any user or platform fallback outside the explicit sandbox grant.
        return await AgentDispatcher(deps).dispatch(SpecAgent(spec), task, instruction=message)

    def _with_gateway(
        self,
        deps: StageDeps,
        record: SandboxRecord,
        gateway: ToolGateway,
        *,
        triggered_by: str,
    ) -> StageDeps:
        """A copy of the shared deps whose tool dispatch is fenced by this sandbox."""
        from devai.tools.dispatch import ToolDispatcher

        dispatcher = ToolDispatcher(
            deps.scm,
            dry_run=True,
            triggered_by=triggered_by,
            gateway=gateway,
        )
        return replace(deps, extra={"tool_dispatcher": dispatcher})

    def _ttl(self, record: SandboxRecord) -> int:
        remaining = int((record.expires_at - datetime.now(UTC)).total_seconds())
        return max(remaining, _MIN_TTL_SECONDS)


def _tool_step(rec: ToolCallRecord) -> TraceStep:
    return TraceStep(
        kind="tool",
        name=rec.tool,
        input=rec.arguments,
        output=rec.response,
        mode=rec.mode.value,
        latency_ms=rec.latency_ms,
        error=rec.error or "",
    )


__all__ = ["SandboxInvoker"]
