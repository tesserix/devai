"""Tesserix ADK runtime bridge for DevAI specializations."""

from __future__ import annotations

import base64
import importlib.metadata
import json
from collections.abc import AsyncIterator, Callable, Sequence
from typing import TYPE_CHECKING, Any, Protocol

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
from devai.adapters.telemetry.runtime import get_global_telemetry
from devai.agentruntime.runner import AgentRunResult
from devai.mcphub.downstream import DownstreamConnection
from devai.mcphub.model import DownstreamSpec, namespaced
from devai.pipeline.types import DevAITask
from devai.specializations.base import HandoverField, Specialization
from devai.tools.dispatch import ToolDispatcher

if TYPE_CHECKING:
    from tesserix_adk.core.streaming import StreamEvent


class MCPConnection(Protocol):
    @property
    def name(self) -> str: ...

    async def connect(self) -> None: ...

    async def list_tools(self) -> list[dict[str, Any]]: ...

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any: ...

    async def close(self) -> None: ...


MCPConnectionFactory = Callable[[DownstreamSpec], MCPConnection]

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
    """Expose one specialization's local and Registry-resolved MCP tools."""

    def __init__(
        self,
        dispatcher: ToolDispatcher,
        allowed: list[str],
        *,
        mcp_endpoints: object = None,
        connection_factory: MCPConnectionFactory = DownstreamConnection,
    ) -> None:
        self._dispatcher = dispatcher
        self._tools = list(_tool_declaration(spec) for spec in dispatcher.build_tool_specs(allowed))
        self._mcp_specs = _mcp_specs(mcp_endpoints)
        self._connection_factory = connection_factory
        self._connections: list[MCPConnection] = []
        self._mcp_tools: dict[str, tuple[MCPConnection, str]] = {}
        self._telemetry = get_global_telemetry()

    async def connect(self) -> None:
        try:
            for spec in self._mcp_specs:
                connection = self._connection_factory(spec)
                self._connections.append(connection)
                attributes = {
                    "mcp.server": spec.name,
                    "mcp.transport": spec.transport,
                    "mcp.routed_via": spec.labels.get("routed_via", ""),
                }
                with self._telemetry.span("mcp.connect", attributes=attributes):
                    await connection.connect()
                    discovered = await connection.list_tools()
                for tool in discovered:
                    wire_name = tool.get("name")
                    if not isinstance(wire_name, str) or not wire_name:
                        raise RuntimeError("MCP server returned an invalid tool")
                    name = namespaced(spec.name, wire_name)
                    if name in self._mcp_tools:
                        raise RuntimeError("MCP server returned a duplicate tool")
                    description = tool.get("description")
                    parameters = tool.get("inputSchema")
                    self._tools.append(
                        ToolDeclaration(
                            name=name,
                            description=description if isinstance(description, str) else "",
                            parameters=(dict(parameters) if isinstance(parameters, dict) else {"type": "object"}),
                        )
                    )
                    self._mcp_tools[name] = (connection, wire_name)
        except Exception as error:
            await self.close()
            raise RuntimeError("required MCP capability is unavailable") from error

    async def close(self) -> None:
        while self._connections:
            connection = self._connections.pop()
            await connection.close()

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(tool.name for tool in self._tools)

    def declarations(self) -> tuple[ToolDeclaration, ...]:
        return tuple(self._tools)

    async def invoke(self, name: str, arguments: Any) -> str:
        if not isinstance(arguments, dict):
            raise TypeError("tool arguments must be an object")
        remote = self._mcp_tools.get(name)
        if remote is not None:
            connection, wire_name = remote
            attributes = {
                "tool.name": name,
                "tool.transport": "mcp",
                "mcp.server": connection.name,
            }
            with self._telemetry.span("tool.call", attributes=attributes):
                return _mcp_result(await connection.call_tool(wire_name, dict(arguments)))
        with self._telemetry.span(
            "tool.call",
            attributes={"tool.name": name, "tool.transport": "local"},
        ):
            return await self._dispatcher.execute(name, dict(arguments))


class TesserixSpecRuntime:
    """Run one DevAI specialization through Tesserix ADK."""

    def __init__(
        self,
        *,
        llm: LLMAdapter,
        dispatcher: ToolDispatcher,
        mcp_connection_factory: MCPConnectionFactory = DownstreamConnection,
    ) -> None:
        self._provider = DevAILLMProvider(llm)
        self._dispatcher = dispatcher
        self._mcp_connection_factory = mcp_connection_factory

    async def run(
        self,
        spec: Specialization,
        task: DevAITask,
        *,
        system_prompt: str,
        user_prompt: str,
        images: list[dict[str, str]] | None = None,
    ) -> AgentRunResult:
        tenant, user = _identity_for(task)
        attributes = {
            "devai.agent": spec.name,
            "devai.run_id": task.id,
            "devai.trace_id": task.trace_id or "",
            "devai.tenant_id": tenant,
            "devai.runtime": "tesserix-adk",
        }
        with get_global_telemetry().span("agent.run", attributes=attributes) as agent_span:
            tools = DevAIToolRegistry(
                self._dispatcher,
                list(spec.allowed_tools),
                mcp_endpoints=task.agent_context.get("mcp_endpoints"),
                connection_factory=self._mcp_connection_factory,
            )
            await tools.connect()
            try:
                definition = definition_for_specialization(spec, tools=tools.names)
                runner = TesserixAgentRunner(
                    provider=self._provider,
                    tools=tools if tools.names else None,
                    max_iterations=min(max(1, spec.max_turns), 60),
                )
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
            finally:
                await tools.close()

            set_attribute = getattr(agent_span, "set_attribute", None)
            if callable(set_attribute):
                set_attribute("devai.status", run.state.value)
                set_attribute(
                    "devai.model_calls",
                    sum(event.kind is RunEventKind.MODEL_CALL for event in run.events),
                )
                set_attribute(
                    "devai.tool_calls",
                    sum(event.kind is RunEventKind.TOOL_CALL for event in run.events),
                )
                set_attribute("gen_ai.usage.input_tokens", run.usage.input_tokens)
                set_attribute("gen_ai.usage.output_tokens", run.usage.output_tokens)

        trace = [
            {
                "kind": "runtime",
                "runtime": "tesserix-adk",
                "version": _ADK_VERSION,
                "run_id": task.id,
                "trace_id": task.trace_id or "",
                "agent": spec.name,
                "status": run.state.value,
            },
            *[_event_trace(event) for event in run.events],
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


def _mcp_specs(value: object) -> tuple[DownstreamSpec, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError("MCP endpoints must be a list")

    specs: list[DownstreamSpec] = []
    for endpoint in value:
        if not isinstance(endpoint, dict):
            raise ValueError("MCP endpoint must be an object")
        name = endpoint.get("name")
        url = endpoint.get("endpoint")
        routed_via = endpoint.get("routed_via")
        if not isinstance(name, str) or not name or not isinstance(url, str) or not url:
            raise ValueError("MCP endpoint identity is incomplete")
        if routed_via not in {"agentgateway", "direct"}:
            raise ValueError("MCP endpoint route is invalid")
        transport = endpoint.get("transport")
        if transport is None:
            transport = endpoint.get("type")
        if transport not in {"streamable-http", "sse"}:
            transport = "streamable-http"
        specs.append(
            DownstreamSpec(
                name=name,
                endpoint=url,
                transport=transport,
                labels={"routed_via": routed_via},
            )
        )
    return tuple(specs)


def _mcp_result(value: Any) -> str:
    if isinstance(value, str):
        encoded = value
    elif hasattr(value, "model_dump"):
        encoded = json.dumps(value.model_dump(mode="json"), separators=(",", ":"))
    else:
        encoded = json.dumps(value, default=str, separators=(",", ":"))
    maximum = 100_000
    if len(encoded) <= maximum:
        return encoded
    return f"{encoded[:maximum]}...[truncated]"


def _event_trace(event: Any) -> dict[str, Any]:
    step: dict[str, Any] = {"kind": event.kind.value, "name": event.name or ""}
    if event.at is not None:
        step["at"] = event.at
    if event.usage is not None:
        step["usage"] = {
            "input_tokens": event.usage.input_tokens,
            "output_tokens": event.usage.output_tokens,
            "cached_tokens": event.usage.cached_tokens,
        }
    return step


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
