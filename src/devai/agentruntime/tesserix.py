"""Tesserix ADK runtime bridge for DevAI specializations."""

from __future__ import annotations

import base64
import importlib.metadata
from collections.abc import AsyncIterator, Sequence
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, create_model
from tesserix_adk.core import (
    Agent,
    AgentDefinition,
    BinaryPart,
    BudgetLimits,
    Message,
    ModelCapabilities,
    ModelRequest,
    ModelResponse,
    Owner,
    ProviderError,
    RunEventKind,
    RunState,
    StopReason,
    TextPart,
    ToolCall,
    ToolDeclaration,
    Usage,
)
from tesserix_adk.runtime import AgentRunner as TesserixAgentRunner

from devai.adapters.llm.base import (
    LLMAdapter,
    LLMMessage,
    LLMRequest,
    LLMRole,
    ToolSpec,
)
from devai.adapters.llm.base import (
    ToolCall as DevAIToolCall,
)
from devai.agentruntime.runner import AgentRunResult
from devai.pipeline.types import DevAITask
from devai.specializations.base import HandoverField, Specialization
from devai.tools.dispatch import ToolDispatcher

if TYPE_CHECKING:
    from tesserix_adk.core.streaming import StreamEvent

_ADK_VERSION = importlib.metadata.version("tesserix-adk")
_OWNER_CONTACT = "https://github.com/tesserix/devai/issues"


class DevAILLMProvider:
    """Expose a request-scoped DevAI LLM chain as an ADK model provider."""

    def __init__(self, adapter: LLMAdapter) -> None:
        self._adapter = adapter

    @property
    def name(self) -> str:
        return str(getattr(self._adapter, "provider_name", "devai") or "devai")

    @property
    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(
            structured_output=True,
            tool_calling=True,
            vision=True,
            streaming=False,
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        system: list[str] = []
        messages: list[LLMMessage] = []
        tool_names: dict[str, str] = {}

        for message in request.messages:
            text = "\n".join(part.text for part in message.content if isinstance(part, TextPart))
            if message.role == "system":
                system.append(text)
                continue
            calls = [
                DevAIToolCall(id=call.id, name=call.name, arguments=dict(call.arguments)) for call in message.tool_calls
            ]
            for call in calls:
                tool_names[call.id] = call.name
            images = [
                {
                    "media_type": part.media_type,
                    "data": base64.b64encode(part.data).decode("ascii"),
                }
                for part in message.content
                if not isinstance(part, TextPart)
            ]
            messages.append(
                LLMMessage(
                    role=LLMRole.parse(message.role),
                    content=text,
                    name=tool_names.get(message.tool_call_id or "", ""),
                    tool_call_id=message.tool_call_id or "",
                    tool_calls=calls,
                    images=images,
                )
            )

        response = await self._adapter.generate(
            LLMRequest(
                system="\n\n".join(system),
                messages=messages,
                tools=[
                    ToolSpec(name=tool.name, description=tool.description, parameters=dict(tool.parameters))
                    for tool in request.tools
                ],
                model="" if request.model == "devai-auto" else request.model,
                response_format={"type": "json_object"} if request.output_schema is not None else None,
            )
        )
        if response.finish_reason == "error":
            raise ProviderError("all authorized DevAI LLM providers failed", provider=self.name)

        return ModelResponse(
            content=response.text,
            tool_calls=tuple(
                ToolCall(id=call.id, name=call.name, arguments=dict(call.arguments)) for call in response.tool_calls
            ),
            usage=Usage(
                input_tokens=response.usage.prompt_tokens,
                output_tokens=response.usage.completion_tokens,
                cached_tokens=response.usage.cached_tokens,
            ),
            stop_reason=_stop_reason(response.finish_reason, bool(response.tool_calls)),
        )

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        del request
        raise NotImplementedError("streaming is not declared by the DevAI ADK bridge")

    def count_tokens(self, messages: Sequence[Message]) -> int:
        characters = sum(
            len(part.text) for message in messages for part in message.content if isinstance(part, TextPart)
        )
        return max(1, characters // 4)


class DevAIToolRegistry:
    """Expose one specialization's existing tool dispatcher to the ADK loop."""

    def __init__(self, dispatcher: ToolDispatcher, allowed: list[str]) -> None:
        self._dispatcher = dispatcher
        self._tools = tuple(_tool_declaration(spec) for spec in dispatcher.build_tool_specs(allowed))

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(tool.name for tool in self._tools)

    def declarations(self) -> tuple[ToolDeclaration, ...]:
        return self._tools

    async def invoke(self, name: str, arguments: Any) -> str:
        if not isinstance(arguments, dict):
            raise TypeError("tool arguments must be an object")
        return await self._dispatcher.execute(name, dict(arguments))


class TesserixSpecRuntime:
    """Run one DevAI specialization through Tesserix ADK."""

    def __init__(self, *, llm: LLMAdapter, dispatcher: ToolDispatcher) -> None:
        self._provider = DevAILLMProvider(llm)
        self._tools = DevAIToolRegistry(dispatcher, [])
        self._dispatcher = dispatcher

    async def run(
        self,
        spec: Specialization,
        task: DevAITask,
        *,
        system_prompt: str,
        user_prompt: str,
        images: list[dict[str, str]] | None = None,
    ) -> AgentRunResult:
        tools = DevAIToolRegistry(self._dispatcher, list(spec.allowed_tools))
        definition = definition_for_specialization(spec, tools=tools.names)
        runner = TesserixAgentRunner(
            provider=self._provider,
            tools=tools if tools.names else None,
            max_iterations=min(max(1, spec.max_turns), 60),
        )
        tenant, user = _identity_for(task)
        if system_prompt.strip() != definition.agent.instructions:
            definition = definition.model_copy(
                update={
                    "agent": definition.agent.model_copy(update={"instructions": system_prompt.strip()}),
                }
            )
        run = await runner.run(
            definition,
            user_prompt,
            tenant=tenant,
            user=user,
            run_id=f"{task.id}:{spec.name}",
            history=_image_history(images or []),
        )

        trace = [
            {"kind": "runtime", "runtime": "tesserix-adk", "version": _ADK_VERSION},
            *[{"kind": event.kind.value, "name": event.name or ""} for event in run.events],
        ]
        output = run.output.model_dump(mode="json") if isinstance(run.output, BaseModel) else {}
        final_text = _last_assistant_text(run.messages)
        return AgentRunResult(
            patch=output,
            final_text=final_text,
            turns=sum(event.kind is RunEventKind.MODEL_CALL for event in run.events),
            tool_calls=sum(event.kind is RunEventKind.TOOL_CALL for event in run.events),
            prompt_tokens=run.usage.input_tokens,
            completion_tokens=run.usage.output_tokens,
            trace_steps=trace,
            error="" if run.state is RunState.COMPLETED else f"adk_{run.state.value}",
        )


def definition_for_specialization(
    spec: Specialization,
    *,
    tools: tuple[str, ...] | None = None,
) -> AgentDefinition[Any]:
    """Translate one specialization into a reviewable ADK definition."""

    output_type = _output_type(spec)
    allowed = tuple(spec.allowed_tools) if tools is None else tools
    max_turns = min(max(1, spec.max_turns), 60)
    agent = Agent(
        name=spec.name.replace("_", "-"),
        version=str(spec.metadata.get("version", "1.0.0")),
        instructions=spec.system_prompt.strip()
        or spec.description.strip()
        or f"Act as {spec.display_name or spec.name}.",
        model=spec.llm_model or "devai-auto",
        tools=allowed,
        output_type=output_type,
        budget=BudgetLimits(
            max_input_tokens=max_turns * 100_000,
            max_output_tokens=max_turns * (spec.max_tokens or 4_096),
            max_model_calls=max_turns,
            max_tool_calls=max_turns * 4,
            max_iterations=max_turns,
            max_seconds=float(spec.timeout_seconds),
        ),
        metadata={
            "category": spec.category,
            "risk_level": spec.risk_level.value,
            "output_key": spec.output_key,
        },
    )
    return AgentDefinition.declared(
        agent=agent,
        owner=Owner(
            team=str(spec.metadata.get("owner") or "devai-team"),
            contact=_OWNER_CONTACT,
            service="devai",
        ),
        evaluation_suite=f"evals/{spec.name.replace('_', '-')}.yaml",
        known_tools=frozenset(allowed),
        instructions_ref=f"specializations/{spec.category}/{spec.name}.yaml",
        metadata={
            "category": spec.category,
            "risk_level": spec.risk_level.value,
            "role_color": spec.role_color,
        },
    )


def _output_type(spec: Specialization) -> type[BaseModel]:
    fields: dict[str, Any] = {}
    for name, field in spec.handover_schema.items():
        annotation = _field_type(field)
        fields[name] = (annotation, ...) if field.required else (annotation | None, None)
    if not fields:
        fields["text"] = (str, ...)
    model_name = "".join(part.title() for part in spec.name.split("_")) + "Handover"
    model: type[BaseModel] = create_model(
        model_name,
        __config__=ConfigDict(frozen=True, extra="forbid"),
        **fields,
    )
    return model


def _field_type(field: HandoverField) -> Any:
    return {
        "string": str,
        "integer": int,
        "number": float,
        "boolean": bool,
        "array": list[Any],
        "object": dict[str, Any],
        "any": Any,
    }[field.type]


def _tool_declaration(spec: ToolSpec) -> ToolDeclaration:
    return ToolDeclaration(name=spec.name, description=spec.description, parameters=dict(spec.parameters))


def _stop_reason(reason: str, has_tool_calls: bool) -> StopReason:
    if has_tool_calls:
        return StopReason.TOOL_CALLS
    return {
        "length": StopReason.MAX_TOKENS,
        "max_tokens": StopReason.MAX_TOKENS,
        "content_filter": StopReason.SAFETY,
        "refusal": StopReason.REFUSAL,
        "stop": StopReason.END_TURN,
        "end_turn": StopReason.END_TURN,
    }.get(reason, StopReason.END_TURN)


def _identity_for(task: DevAITask) -> tuple[str, str | None]:
    principal = task.principal or {}
    tenant = str(principal.get("tenant_id") or task.team_id or "devai")
    user = str(principal.get("uid") or principal.get("email") or task.triggered_by or "") or None
    return tenant, user


def _last_assistant_text(messages: Sequence[Message]) -> str:
    for message in reversed(messages):
        if message.role == "assistant":
            text = "".join(part.text for part in message.content if isinstance(part, TextPart))
            prefix = '<untrusted-data source="model_output">\n'
            suffix = "\n</untrusted-data>"
            if text.startswith(prefix) and text.endswith(suffix):
                return text[len(prefix) : -len(suffix)]
            return text
    return ""


def _image_history(images: list[dict[str, str]]) -> tuple[Message, ...]:
    if not images:
        return ()
    parts: list[TextPart | BinaryPart] = [
        BinaryPart(
            media_type=image["media_type"],
            data=base64.b64decode(image["data"], validate=True),
        )
        for image in images
    ]
    return (Message(role="user", content=parts),)


__all__ = [
    "DevAILLMProvider",
    "DevAIToolRegistry",
    "TesserixSpecRuntime",
    "definition_for_specialization",
]
