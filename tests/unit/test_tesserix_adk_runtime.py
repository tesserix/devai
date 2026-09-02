from __future__ import annotations

import base64
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from tesserix_adk.core import Message, ModelRequest, RunEventKind, StopReason, TextPart

from devai.adapters.llm.base import LLMAdapter, LLMRequest, LLMResponse, LLMUsage, ToolCall, ToolSpec
from devai.adapters.telemetry.noop import NoopTelemetryAdapter
from devai.adapters.telemetry.runtime import set_global_telemetry
from devai.agentruntime.tesserix import DevAILLMProvider, TesserixSpecRuntime, definition_for_specialization
from devai.pipeline.types import DevAITask
from devai.specializations.base import HandoverField, Specialization
from devai.specializations.loader import discover_specializations
from devai.tools.dispatch import ToolDispatcher


class ScriptedMCPConnection:
    def __init__(self, spec: Any) -> None:
        self.spec = spec
        self.connected = False
        self.closed = False
        self.calls: list[tuple[str, dict[str, object]]] = []

    @property
    def name(self) -> str:
        return str(self.spec.name)

    async def connect(self) -> None:
        self.connected = True

    async def list_tools(self) -> list[dict[str, object]]:
        return [
            {
                "name": "get_ticket",
                "description": "Read one issue from the configured SCM.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"number": {"type": "integer"}},
                    "required": ["number"],
                },
            }
        ]

    async def call_tool(self, name: str, arguments: dict[str, object]) -> object:
        self.calls.append((name, arguments))
        return {"content": [{"type": "text", "text": "issue is ready"}]}

    async def close(self) -> None:
        self.closed = True


class RecordingTelemetry(NoopTelemetryAdapter):
    def __init__(self) -> None:
        self.spans: list[tuple[str, dict[str, object]]] = []

    @contextmanager
    def span(self, name: str, *, attributes: dict[str, Any] | None = None):
        recorded = dict(attributes or {})
        self.spans.append((name, recorded))

        class Span:
            def set_attribute(self, key: str, value: object) -> None:
                recorded[key] = value

        yield Span()


class ScriptedLLM(LLMAdapter):
    provider_name = "vertex_gemini"
    default_model = "gemini-2.5-flash"

    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = list(responses)
        self.requests: list[LLMRequest] = []

    async def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return self.responses.pop(0)


class ScriptedTools(ToolDispatcher):
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        super().__init__()

    async def execute(self, name: str, arguments: dict[str, object]) -> str:
        self.calls.append((name, arguments))
        return "API is healthy"

    def build_tool_specs(self, names: list[str]) -> list[ToolSpec]:
        return [
            ToolSpec(
                name=name,
                description="Read service logs.",
                parameters={"type": "object", "properties": {"service": {"type": "string"}}},
            )
            for name in names
        ]


def _spec(*, tools: list[str] | None = None) -> Specialization:
    return Specialization(
        name="health_reviewer",
        system_prompt="Review the service health and return JSON.",
        llm_model="gemini-2.5-flash",
        allowed_tools=tools or [],
        max_turns=4,
        timeout_seconds=30,
        handover_schema={
            "summary": HandoverField(name="summary", type="string", required=True),
        },
        metadata={"owner": "sre-team"},
    )


@pytest.mark.asyncio
async def test_devai_provider_translates_adk_request_and_usage() -> None:
    llm = ScriptedLLM(
        [
            LLMResponse(
                text="checking",
                tool_calls=[ToolCall(id="call-1", name="read_status", arguments={"service": "api"})],
                usage=LLMUsage(prompt_tokens=11, completion_tokens=3, cached_tokens=2),
                finish_reason="tool_use",
            )
        ]
    )
    provider = DevAILLMProvider(llm)
    request = ModelRequest(
        model="gemini-2.5-flash",
        messages=(Message(role="user", content=[TextPart(text="Check the API")]),),
    )

    response = await provider.complete(request)

    assert response.stop_reason is StopReason.TOOL_CALLS
    assert response.tool_calls[0].name == "read_status"
    assert response.usage.input_tokens == 11
    assert response.usage.cached_tokens == 2
    assert llm.requests[0].messages[0].content == "Check the API"


def test_definition_preserves_specialization_contract() -> None:
    definition = definition_for_specialization(_spec(tools=["get_logs"]))

    assert definition.agent.name == "health-reviewer"
    assert definition.agent.model == "gemini-2.5-flash"
    assert definition.agent.tools == ("get_logs",)
    assert definition.agent.budget is not None
    assert definition.agent.budget.max_model_calls == 4
    assert definition.agent.budget.max_seconds == 30
    assert definition.output_schema is not None
    assert definition.output_schema["required"] == ["summary"]
    assert definition.metadata["risk_level"] == "medium"


def test_all_catalog_agents_have_valid_adk_definitions() -> None:
    specs = discover_specializations(Path(__file__).resolve().parents[2] / "specializations")

    definitions = {name: definition_for_specialization(spec) for name, spec in specs.items()}

    assert len(definitions) == 41
    assert {definition.agent.name for definition in definitions.values()} == {name.replace("_", "-") for name in specs}
    assert all(definition.revision for definition in definitions.values())


@pytest.mark.asyncio
async def test_runtime_executes_tools_and_returns_typed_handover() -> None:
    llm = ScriptedLLM(
        [
            LLMResponse(
                tool_calls=[ToolCall(id="call-1", name="get_logs", arguments={"service": "api"})],
                usage=LLMUsage(prompt_tokens=7, completion_tokens=2),
                finish_reason="tool_use",
            ),
            LLMResponse(
                text='{"summary":"API is healthy"}',
                usage=LLMUsage(prompt_tokens=9, completion_tokens=4),
                finish_reason="stop",
            ),
        ]
    )
    tools = ScriptedTools()
    runtime = TesserixSpecRuntime(llm=llm, dispatcher=tools)

    result = await runtime.run(
        _spec(tools=["get_logs"]),
        DevAITask(intent="Check production", repo="tesserix/devai", triggered_by="owner@example.com"),
        system_prompt="Review health.",
        user_prompt="Check production",
    )

    assert result.patch == {"summary": "API is healthy"}
    assert result.turns == 2
    assert result.tool_calls == 1
    assert result.prompt_tokens == 16
    assert tools.calls == [("get_logs", {"service": "api"})]
    assert result.trace_steps[0]["runtime"] == "tesserix-adk"
    assert any(step["kind"] == RunEventKind.OUTPUT_VALIDATED.value for step in result.trace_steps)


@pytest.mark.asyncio
async def test_runtime_discovers_and_invokes_gateway_mcp_tools() -> None:
    llm = ScriptedLLM(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="call-mcp",
                        name="scm-mcp__get_ticket",
                        arguments={"number": 42},
                    )
                ]
            ),
            LLMResponse(text='{"summary":"issue is ready"}'),
        ]
    )
    connections: list[ScriptedMCPConnection] = []

    def connection_factory(spec: Any) -> ScriptedMCPConnection:
        connection = ScriptedMCPConnection(spec)
        connections.append(connection)
        return connection

    runtime = TesserixSpecRuntime(
        llm=llm,
        dispatcher=ScriptedTools(),
        mcp_connection_factory=connection_factory,
    )
    task = DevAITask(
        intent="Inspect issue 42",
        triggered_by="owner@example.com",
        agent_context={
            "mcp_endpoints": [
                {
                    "name": "scm-mcp",
                    "endpoint": "https://mcp.example.com/mcp/scm-mcp",
                    "type": "streamable-http",
                    "routed_via": "agentgateway",
                }
            ]
        },
    )

    result = await runtime.run(
        _spec(),
        task,
        system_prompt="Review the issue.",
        user_prompt="Inspect issue 42",
    )

    assert result.patch == {"summary": "issue is ready"}
    assert connections[0].connected
    assert connections[0].closed
    assert connections[0].calls == [("get_ticket", {"number": 42})]
    assert {tool.name for tool in llm.requests[0].tools} == {"scm-mcp__get_ticket"}


@pytest.mark.asyncio
async def test_runtime_emits_correlated_agent_and_mcp_spans() -> None:
    telemetry = RecordingTelemetry()
    set_global_telemetry(telemetry)
    llm = ScriptedLLM(
        [
            LLMResponse(tool_calls=[ToolCall(id="call-mcp", name="scm-mcp__get_ticket", arguments={"number": 7})]),
            LLMResponse(text='{"summary":"traced"}'),
        ]
    )

    try:
        runtime = TesserixSpecRuntime(
            llm=llm,
            dispatcher=ScriptedTools(),
            mcp_connection_factory=ScriptedMCPConnection,
        )
        await runtime.run(
            _spec(),
            DevAITask(
                id="run-7",
                intent="Trace issue",
                trace_id="0123456789abcdef0123456789abcdef",
                principal={"tenant_id": "tenant-a", "uid": "user-a"},
                agent_context={
                    "mcp_endpoints": [
                        {
                            "name": "scm-mcp",
                            "endpoint": "https://mcp.example.com/mcp/scm-mcp",
                            "type": "streamable-http",
                            "routed_via": "agentgateway",
                        }
                    ]
                },
            ),
            system_prompt="Trace the issue.",
            user_prompt="Trace issue 7",
        )
    finally:
        set_global_telemetry(None)

    spans = dict(telemetry.spans)
    assert spans["agent.run"] == {
        "devai.agent": "health_reviewer",
        "devai.run_id": "run-7",
        "devai.trace_id": "0123456789abcdef0123456789abcdef",
        "devai.tenant_id": "tenant-a",
        "devai.runtime": "tesserix-adk",
        "devai.status": "completed",
        "devai.model_calls": 2,
        "devai.tool_calls": 1,
        "gen_ai.usage.input_tokens": 0,
        "gen_ai.usage.output_tokens": 0,
    }
    assert spans["mcp.connect"]["mcp.server"] == "scm-mcp"
    assert spans["tool.call"]["tool.name"] == "scm-mcp__get_ticket"
    assert spans["tool.call"]["tool.transport"] == "mcp"


@pytest.mark.asyncio
async def test_runtime_preserves_image_attachments_for_multimodal_agents() -> None:
    llm = ScriptedLLM([LLMResponse(text='{"summary":"diagram reviewed"}')])
    runtime = TesserixSpecRuntime(llm=llm, dispatcher=ScriptedTools())
    image = base64.b64encode(b"png-bytes").decode("ascii")

    result = await runtime.run(
        _spec(),
        DevAITask(intent="Review diagram", triggered_by="owner@example.com"),
        system_prompt="Review images.",
        user_prompt="Review the attached architecture diagram",
        images=[{"media_type": "image/png", "data": image}],
    )

    assert result.patch == {"summary": "diagram reviewed"}
    assert any(message.images == [{"media_type": "image/png", "data": image}] for message in llm.requests[0].messages)


@pytest.mark.asyncio
async def test_provider_stream_refuses_when_bridge_does_not_declare_streaming() -> None:
    provider = DevAILLMProvider(ScriptedLLM([]))

    with pytest.raises(NotImplementedError, match="streaming"):
        await provider.stream(
            ModelRequest(
                model="gemini-2.5-flash",
                messages=(Message(role="user", content=[TextPart(text="hello")]),),
            )
        )
