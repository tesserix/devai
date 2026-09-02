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

from devai.adapters.llm.base import LLMAdapter, LLMRequest, LLMResponse
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


def _prompt_version(record: SandboxRecord) -> str:
    if record.spec.prompt is not None:
        return record.spec.prompt.version
    if record.spec.agent is not None:
        return record.spec.agent.version
    return "draft"


class SandboxInvoker:
    def __init__(
        self,
        *,
        specializations: SpecializationService,
        deps: StageDeps,
        traces: TraceStore,
        credentials: SandboxCredentialProvider | None = None,
        telemetry: Any | None = None,
        portable_client: Any | None = None,
        registry: Any | None = None,
    ) -> None:
        from devai.sandbox.credentials import SandboxCredentialResolver

        self._specs = specializations
        self._registry = registry
        self._deps = deps
        self._traces = traces
        self._credentials = credentials or SandboxCredentialResolver()
        if portable_client is None:
            from devai.sandbox.portable_client import PortableRuntimeClient

            portable_client = PortableRuntimeClient(deps.config)
        self._portable_client = portable_client
        if telemetry is None:
            from devai.adapters.telemetry.runtime import get_global_telemetry

            telemetry = get_global_telemetry()
        self._telemetry = telemetry

    async def invoke(self, record: SandboxRecord, *, message: str, triggered_by: str) -> Invocation:
        """One turn. Model and tool failures become a failed trace, never a lost one."""
        if record.status not in _LIVE:
            raise ValueError(f"sandbox {record.id} is {record.status.value} and cannot be invoked")
        if record.spec.import_snapshot is not None:
            return await self._invoke_portable(record, message=message, triggered_by=triggered_by)
        spec = await self._resolve(record)
        from devai.services.redact import redact_secrets

        safe_message = redact_secrets(message)

        steps: list[TraceStep] = [
            TraceStep(kind="prompt", name="system", output=redact_secrets(spec.system_prompt)),
            TraceStep(kind="prompt", name="user", output=safe_message),
        ]
        invocation = Invocation(
            id=f"inv-{uuid.uuid4().hex[:12]}",
            sandbox_id=record.id,
            agent=spec.name,
            message=safe_message,
            execution_backend="inline",
            steps=steps,
        )
        gateway = ToolGateway(
            policy=record.spec.tools,
            sink=lambda rec: self._record_tool_step(
                rec,
                steps,
                invocation_id=invocation.id,
                sandbox_id=record.id,
            ),
        )
        started = time.perf_counter()
        with self._telemetry.span(
            "sandbox.invocation",
            attributes={
                "invocation_id": invocation.id,
                "sandbox_id": record.id,
                "agent": spec.name,
                "provider": record.spec.model.provider,
                "model": record.spec.model.model,
            },
        ):
            for step in steps:
                self._mirror_step(step, invocation_id=invocation.id, sandbox_id=record.id)
            try:
                try:
                    async with asyncio.timeout(record.spec.limits.max_wall_clock_s):
                        result = await self._run(
                            spec,
                            record,
                            gateway,
                            steps,
                            invocation_id=invocation.id,
                            message=message,
                            triggered_by=triggered_by,
                        )
                except TimeoutError as exc:
                    from devai.sandbox.credentials import SandboxBudgetExceeded

                    raise SandboxBudgetExceeded(
                        f"sandbox {record.id} exceeded its wall-clock budget ({record.spec.limits.max_wall_clock_s}s)"
                    ) from exc
                safe_final_text = redact_secrets(result.final_text)
                invocation.final_text = safe_final_text
                invocation.ok = result.ok
                invocation.error = redact_secrets(result.error or "")[:500]
                response_step = TraceStep(kind="response", name="final", output=safe_final_text)
                steps.append(response_step)
                self._mirror_step(response_step, invocation_id=invocation.id, sandbox_id=record.id)
            except Exception as exc:  # noqa: BLE001 — a broken run is evidence, not an outage
                from devai.services.redact import redact_secrets

                safe_error = redact_secrets(str(exc))[:500]
                logger.warning("sandbox %s: invocation failed: %s", record.id, safe_error)
                invocation.ok = False
                invocation.error = safe_error
                failure_step = TraceStep(
                    kind="llm",
                    name=record.spec.model.model,
                    provider=record.spec.model.provider,
                    prompt_version=_prompt_version(record),
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    error=safe_error,
                )
                steps.append(failure_step)
                self._mirror_step(failure_step, invocation_id=invocation.id, sandbox_id=record.id)

        invocation.wall_clock_ms = max(1, int((time.perf_counter() - started) * 1000))
        await self._traces.save(invocation, ttl_seconds=self._ttl(record))
        return invocation

    async def _invoke_portable(self, record: SandboxRecord, *, message: str, triggered_by: str) -> Invocation:
        from devai.services.redact import redact_secrets

        if record.spec.agent is None:
            raise ValueError(f"sandbox {record.id} has no imported agent identity")
        safe_message = redact_secrets(message)
        invocation = Invocation(
            id=f"inv-{uuid.uuid4().hex[:12]}",
            sandbox_id=record.id,
            agent=record.spec.agent.name,
            message=safe_message,
            execution_backend="portable",
            steps=[TraceStep(kind="prompt", name="user", output=safe_message)],
        )
        started = time.perf_counter()
        try:
            async with asyncio.timeout(record.spec.limits.max_wall_clock_s):
                result = await self._portable_client.invoke(
                    record,
                    message=message,
                    triggered_by=triggered_by,
                )
            invocation.final_text = redact_secrets(result.final_text)
            invocation.execution_backend = result.backend
            invocation.steps.append(TraceStep(kind="response", name="final", output=invocation.final_text))
        except Exception as exc:  # noqa: BLE001
            invocation.ok = False
            invocation.error = redact_secrets(str(exc))[:500]
            invocation.steps.append(TraceStep(kind="response", name="final", error=invocation.error))
            logger.warning("sandbox %s: portable invocation failed: %s", record.id, invocation.error)
        invocation.wall_clock_ms = int((time.perf_counter() - started) * 1000)
        for step in invocation.steps:
            self._mirror_step(step, invocation_id=invocation.id, sandbox_id=record.id)
        await self._traces.save(invocation, ttl_seconds=self._ttl(record))
        return invocation

    async def _resolve(self, record: SandboxRecord) -> Specialization:
        from devai.registry.mapping import agent_envelope_to_spec, role_name

        spec: Specialization | None
        if record.spec.draft:
            spec = agent_envelope_to_spec(record.spec.draft)
        else:
            if record.spec.agent is None:
                raise ValueError(f"sandbox {record.id} has no agent")
            from devai.specializations.service import GovernedAgentError

            try:
                spec = await self._specs.resolve_runnable(role_name(record.spec.agent.name))
            except GovernedAgentError as exc:
                # Governed admission protects live runs; inside the sandbox fence
                # (mock tools, dry run, budget) an inadmissible reviewed bundle
                # degrades to the registry envelope, same as an unknown role.
                logger.warning(
                    "sandbox %s: governed resolution of %s failed (%s); trying registry envelope",
                    record.id,
                    record.spec.agent.name,
                    exc,
                )
                spec = None
            if spec is None:
                spec = await self._registry_spec(record.spec.agent.name)
        if spec is None:
            agent_name = record.spec.agent.name if record.spec.agent is not None else "draft"
            raise ValueError(f"sandbox {record.id} pins agent {agent_name!r}, which is not runnable here")
        # The sandbox's pin beats the role's own preference — that is what makes
        # a result attributable to a configuration rather than to a default.
        return replace(
            spec,
            llm_model=record.spec.model.model or spec.llm_model,
            max_turns=min(spec.max_turns, 8),
        )

    async def _registry_spec(self, name: str) -> Specialization | None:
        """User-published catalog records aren't in the reviewed role catalog;
        run them from their registry envelope — the same trust a draft already
        gets, and the sandbox fence (mock tools, dry run, budget) still applies."""
        if self._registry is None:
            return None
        from devai.registry.mapping import agent_envelope_to_spec

        try:
            agent = await asyncio.to_thread(self._registry.get_agent, name)
        except Exception:  # noqa: BLE001 — registry outage degrades to "not runnable"
            logger.warning("sandbox: registry lookup failed for agent %s", name, exc_info=True)
            return None
        raw = getattr(agent, "raw", None)
        if not raw:
            return None
        try:
            return agent_envelope_to_spec(raw)
        except ValueError:
            logger.warning("sandbox: registry envelope for %s is not runnable", name)
            return None

    async def _run(
        self,
        spec: Specialization,
        record: SandboxRecord,
        gateway: ToolGateway,
        steps: list[TraceStep],
        *,
        invocation_id: str,
        message: str,
        triggered_by: str,
    ) -> Any:
        from devai.agentruntime import AgentDispatcher, SpecAgent
        from devai.pipeline.types import DevAITask

        deps = await self._credentials.resolve(record, self._deps)
        if deps.llm is not None:
            deps = replace(
                deps,
                llm=_TracingLLMAdapter(
                    deps.llm,
                    steps=steps,
                    invocation_id=invocation_id,
                    provider=record.spec.model.provider,
                    model=record.spec.model.model,
                    prompt_version=_prompt_version(record),
                    telemetry=self._telemetry,
                ),
            )
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

    def _record_tool_step(
        self,
        record: ToolCallRecord,
        steps: list[TraceStep],
        *,
        invocation_id: str,
        sandbox_id: str,
    ) -> None:
        step = _tool_step(record)
        steps.append(step)
        self._mirror_step(step, invocation_id=invocation_id, sandbox_id=sandbox_id)

    def _mirror_step(self, step: TraceStep, *, invocation_id: str, sandbox_id: str) -> None:
        attributes: dict[str, Any] = {
            "invocation_id": invocation_id,
            "sandbox_id": sandbox_id,
            "kind": step.kind,
            "name": step.name,
            "status": "error" if step.error else "ok",
            "latency_ms": step.latency_ms,
        }
        if step.provider:
            attributes["provider"] = step.provider
        if step.prompt_version:
            attributes["prompt_version"] = step.prompt_version
        if step.mode:
            attributes["mode"] = step.mode
        with self._telemetry.span(f"sandbox.{step.kind}", attributes=attributes):
            pass


class _TracingLLMAdapter(LLMAdapter):
    def __init__(
        self,
        inner: LLMAdapter,
        *,
        steps: list[TraceStep],
        invocation_id: str,
        provider: str,
        model: str,
        prompt_version: str,
        telemetry: Any,
    ) -> None:
        self._inner = inner
        self._steps = steps
        self._invocation_id = invocation_id
        self._provider = provider
        self._model = model
        self._prompt_version = prompt_version
        self._telemetry = telemetry
        self.provider_name = provider
        self.default_model = model

    async def generate(self, request: LLMRequest) -> LLMResponse:
        started_at = datetime.now(UTC).isoformat()
        started = time.perf_counter()
        with self._telemetry.span(
            "sandbox.llm",
            attributes={
                "invocation_id": self._invocation_id,
                "provider": self._provider,
                "model": request.model or self._model,
                "prompt_version": self._prompt_version,
            },
        ) as span:
            try:
                response = await self._inner.generate(request)
            except Exception as exc:
                from devai.sandbox.credentials import SandboxBudgetExceeded
                from devai.services.redact import redact_secrets

                if isinstance(exc, SandboxBudgetExceeded) and exc.response is not None:
                    step = self._append_response(
                        exc.response,
                        request,
                        started=started,
                        started_at=started_at,
                        error=redact_secrets(str(exc))[:500],
                    )
                    _set_span_attributes(span, step)
                    raise
                step = TraceStep(
                    kind="llm",
                    name=request.model or self._model,
                    provider=self._provider,
                    prompt_version=self._prompt_version,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    error=redact_secrets(str(exc))[:500],
                    started_at=started_at,
                )
                self._steps.append(step)
                _set_span_attributes(span, step)
                raise

            step = self._append_response(response, request, started=started, started_at=started_at)
            _set_span_attributes(span, step)
        return response

    def _append_response(
        self,
        response: LLMResponse,
        request: LLMRequest,
        *,
        started: float,
        started_at: str,
        error: str = "",
    ) -> TraceStep:
        from devai.analytics.pricing import estimate_cost
        from devai.services.redact import redact_secrets

        provider = self._provider
        model = response.model or request.model or self._model
        prompt_tokens = int(response.usage.prompt_tokens or 0)
        completion_tokens = int(response.usage.completion_tokens or 0)
        estimated = estimate_cost(provider, model, prompt_tokens, completion_tokens)
        cost_usd = float(response.extra.get("sandbox_call_cost_usd", estimated))
        step = TraceStep(
            kind="llm",
            name=model,
            provider=provider,
            prompt_version=self._prompt_version,
            output=redact_secrets(response.text),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost_usd,
            latency_ms=int(response.latency_ms or (time.perf_counter() - started) * 1000),
            error=error,
            started_at=started_at,
        )
        self._steps.append(step)
        return step

    async def embed(self, texts: list[str], *, model: str = "") -> list[list[float]]:
        return await self._inner.embed(texts, model=model)

    async def list_models(self) -> list[dict[str, str]]:
        return await self._inner.list_models()

    async def health_check(self) -> dict[str, Any]:
        return await self._inner.health_check()


def _set_span_attributes(span: Any, step: TraceStep) -> None:
    if span is None or not hasattr(span, "set_attribute"):
        return
    for key, value in {
        "provider": step.provider,
        "model": step.name,
        "prompt_version": step.prompt_version,
        "tokens_input": step.prompt_tokens,
        "tokens_output": step.completion_tokens,
        "cost_usd": step.cost_usd,
        "latency_ms": step.latency_ms,
        "status": "error" if step.error else "ok",
    }.items():
        span.set_attribute(key, value)


def _tool_step(rec: ToolCallRecord) -> TraceStep:
    from devai.services.redact import redact_secrets

    return TraceStep(
        kind="tool",
        name=rec.tool,
        input=rec.arguments,
        output=rec.response,
        mode=rec.mode.value,
        latency_ms=rec.latency_ms,
        error=redact_secrets(rec.error or "")[:500],
    )


__all__ = ["SandboxInvoker"]
