"""Contract tests for the unified Agent SDK + ADK (``devai.agentruntime``).

These prove the Phase-1 foundation without touching any live execution path:

  - ``LegacyAgent`` maps a RunContext → ALMState, runs a ``BaseAgent``-shaped
    agent, and wraps the patch in a typed ``AgentResult`` (≡ today's adapter).
  - ``AgentResult.to_stage_result`` nests the handover, surfaces well-known
    scalars, and mirrors structural fields onto the task.
  - ``SpecAgent`` runs a YAML spec through the canonical tool loop, and routes a
    ``legacy_python_class`` spec through ``LegacyAgent``.
  - ``AgentDispatcher`` resolves the context and runs via ``InlineBackend``,
    ``dispatch_many`` fans out, and ``ctx.spawn`` recurses into a sub-agent.
  - both concrete agents satisfy the ``Agent`` protocol.
"""

from __future__ import annotations

import asyncio

import pytest

from devai.adapters.llm.base import LLMResponse, LLMUsage, ToolCall
from devai.agentruntime import (
    Agent,
    AgentDispatcher,
    AgentResult,
    InlineBackend,
    LegacyAgent,
    RunContext,
    SpecAgent,
)
from devai.pipeline.interfaces import StageDeps
from devai.pipeline.types import DevAITask
from devai.specializations.base import HandoverField, Specialization

# ─── doubles ─────────────────────────────────────────────────────────────────


class FakeLLMAdapter:
    """Scripts a list of LLMResponses (same shape as test_agent_runner)."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.requests: list = []

    async def generate(self, request):  # noqa: ANN001
        self.requests.append(request)
        if not self._responses:
            return LLMResponse(text='{"done": true}', usage=LLMUsage())
        return self._responses.pop(0)


class FakeBaseAgent:
    """A minimal BaseAgent-shaped object: ``name`` + async ``run(state)->dict``."""

    name = "requirements_analyst"

    def __init__(self) -> None:
        self.seen_state: dict | None = None

    async def run(self, state):  # noqa: ANN001
        self.seen_state = state
        return {
            "summary": "analyzed",
            "pr_number": 42,
            "a2a_messages": [{"id": "m1", "type": "notification"}],
        }


class FakeAgent:
    """A native agent implementing the SDK protocol directly."""

    def __init__(self, name: str) -> None:
        self.name = name

    async def run(self, ctx: RunContext) -> AgentResult:
        return AgentResult(handover={"who": self.name, "intent": ctx.task.intent})


def _deps(llm=None, scm=None) -> StageDeps:  # noqa: ANN001
    return StageDeps(config=None, scm=scm, state_manager=None, llm=llm)


def _task(**kw) -> DevAITask:
    return DevAITask(intent=kw.pop("intent", "do a thing"), repo=kw.pop("repo", "tesserix/x"), **kw)


# ─── LegacyAgent ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_legacy_agent_wraps_base_agent_and_builds_state():
    agent = FakeBaseAgent()
    legacy = LegacyAgent(agent)
    task = _task(intent="ship the feature")
    ctx = RunContext(task=task, deps=_deps())

    result = await legacy.run(ctx)

    # the wrapped agent saw a correctly-built ALMState slice
    assert agent.seen_state is not None
    assert agent.seen_state["run_id"] == task.id
    assert agent.seen_state["requirements"] == "ship the feature"
    assert agent.seen_state["repo_full_name"] == "tesserix/x"

    # the patch came back as a typed result
    assert result.ok is True
    assert result.output_key == "requirements_analyst_output"
    assert result.handover["summary"] == "analyzed"
    assert result.a2a_messages == [{"id": "m1", "type": "notification"}]


@pytest.mark.asyncio
async def test_legacy_agent_to_stage_result_nests_and_mirrors():
    legacy = LegacyAgent(FakeBaseAgent())
    task = _task()
    result = await legacy.run(RunContext(task=task, deps=_deps()))

    stage_result = result.to_stage_result(task)

    # nested under the role key for downstream readers …
    assert stage_result.data["requirements_analyst_output"]["summary"] == "analyzed"
    # … and the well-known scalar surfaced flat …
    assert stage_result.data["pr_number"] == 42
    # … A2A traffic preserved …
    assert stage_result.data["a2a_messages"] == [{"id": "m1", "type": "notification"}]
    # … and the structural field mirrored onto the task object
    assert task.pr_number == 42


@pytest.mark.asyncio
async def test_legacy_agent_degrades_to_stub_on_bad_import():
    legacy = LegacyAgent.from_dotted("devai.nope.DoesNotExist", name="ghost")
    result = await legacy.run(RunContext(task=_task(), deps=_deps()))
    assert result.ok is False
    assert result.stub is True
    assert result.handover == {"ghost_stub": True}


class _FourArgAgent:
    """A BaseAgent-shaped class taking the (scm, state_manager, config, event_bus)
    constructor — used to exercise the require_deps construction guard."""

    name = "four_arg"

    def __init__(self, scm, state_manager, config, event_bus) -> None:  # noqa: ANN001
        self.args = (scm, state_manager, config, event_bus)

    async def run(self, state):  # noqa: ANN001
        return {"ran": True}


@pytest.mark.asyncio
async def test_legacy_agent_require_deps_true_stubs_without_state_manager():
    # Default: a Redis-less context (state_manager=None) degrades to a stub.
    legacy = LegacyAgent.from_class(_FourArgAgent)
    result = await legacy.run(RunContext(task=_task(), deps=_deps()))
    assert result.stub is True


@pytest.mark.asyncio
async def test_legacy_agent_require_deps_false_builds_in_a_jobless_context():
    # The Job runner sets require_deps=False — it constructs even with no
    # state_manager (the agent tolerates None), matching the old reflection path.
    legacy = LegacyAgent.from_class(_FourArgAgent, output_key="four_arg_output", require_deps=False)
    result = await legacy.run(RunContext(task=_task(), deps=_deps()))
    assert result.stub is False
    assert result.handover == {"ran": True}


@pytest.mark.asyncio
async def test_legacy_requirements_agent_uses_resolved_user_llm():
    from devai.agents.requirements_analyst import RequirementsAnalystAgent
    from devai.config import Settings

    llm = FakeLLMAdapter(
        [
            LLMResponse(
                text=('{"requirements":[{"title":"Check health"}],"gaps":[],"overall_assessment":"ready"}'),
                provider="vertex_gemini",
            )
        ]
    )
    config = Settings(
        llm_provider="vertex_gemini",
        llm_require_user_connector=True,
        openai_api_key="",
        anthropic_api_key="",
        vertex_project="user-project",
        vertex_api_key="vertex-user-key",
    )
    legacy = LegacyAgent.from_class(RequirementsAnalystAgent, require_deps=False)

    result = await legacy.run(RunContext(task=_task(), deps=_deps(), llm=llm, config=config))

    assert result.ok is True
    assert result.handover["analyzed_requirements"] == [{"title": "Check health"}]
    assert len(llm.requests) == 1
    assert llm.requests[0].response_format == {"type": "json_object"}


class _LegacyGeminiAgent:
    name = "legacy_gemini"

    def __init__(self, scm, state_manager, config, event_bus) -> None:  # noqa: ANN001
        from devai.providers.gemini_provider import GeminiProvider

        self.provider = GeminiProvider(config)

    async def run(self, state):  # noqa: ANN001
        return {"text": await self.provider.generate("detect stack", system="be concise")}


class _LegacyClaudeAgent:
    name = "legacy_claude"

    def __init__(self, scm, state_manager, config, event_bus) -> None:  # noqa: ANN001
        from devai.providers.anthropic_claude import ClaudeProvider

        self.provider = ClaudeProvider(config)

    async def run(self, state):  # noqa: ANN001
        return {"text": await self.provider.generate("be concise", "review this")}


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_cls", [_LegacyGeminiAgent, _LegacyClaudeAgent])
async def test_other_legacy_provider_facades_use_resolved_user_llm(agent_cls):  # noqa: ANN001
    from devai.config import Settings

    llm = FakeLLMAdapter([LLMResponse(text="dynamic-ok", provider="vertex_gemini")])
    config = Settings(
        llm_provider="vertex_gemini",
        llm_require_user_connector=True,
        openai_api_key="",
        anthropic_api_key="",
        gemini_api_key="",
        vertex_project="user-project",
        vertex_api_key="vertex-user-key",
    )
    legacy = LegacyAgent.from_class(agent_cls, require_deps=False)

    result = await legacy.run(RunContext(task=_task(), deps=_deps(), llm=llm, config=config))

    assert result.handover["text"] == "dynamic-ok"
    assert len(llm.requests) == 1


class _LegacyClaudeToolAgent:
    name = "legacy_claude_tool"

    def __init__(self, scm, state_manager, config, event_bus) -> None:  # noqa: ANN001
        from devai.providers.anthropic_claude import ClaudeProvider

        self.provider = ClaudeProvider(config)

    async def run(self, state):  # noqa: ANN001
        calls = []

        async def execute(name, arguments):  # noqa: ANN001
            calls.append((name, arguments))
            return "healthy"

        text = await self.provider.run_agent_loop(
            "Inspect without changing anything.",
            "Check the API.",
            [
                {
                    "name": "read_status",
                    "description": "Read service status",
                    "input_schema": {
                        "type": "object",
                        "properties": {"service": {"type": "string"}},
                    },
                }
            ],
            execute,
            max_iterations=3,
        )
        return {"text": text, "calls": calls}


@pytest.mark.asyncio
async def test_legacy_claude_tool_loop_uses_resolved_user_llm():
    from devai.config import Settings

    llm = FakeLLMAdapter(
        [
            LLMResponse(
                tool_calls=[ToolCall(id="tool-1", name="read_status", arguments={"service": "api"})],
                finish_reason="tool_use",
                provider="vertex_gemini",
            ),
            LLMResponse(text="API is healthy", finish_reason="stop", provider="vertex_gemini"),
        ]
    )
    config = Settings(
        llm_provider="vertex_gemini",
        llm_require_user_connector=True,
        anthropic_api_key="",
        vertex_project="user-project",
        vertex_api_key="vertex-user-key",
        claude_session_iterations=3,
        claude_max_sessions=1,
    )
    legacy = LegacyAgent.from_class(_LegacyClaudeToolAgent, require_deps=False)

    result = await legacy.run(RunContext(task=_task(), deps=_deps(), llm=llm, config=config))

    assert result.handover["text"] == "API is healthy"
    assert result.handover["calls"] == [("read_status", {"service": "api"})]
    assert len(llm.requests) == 2
    assert llm.requests[0].tools[0].name == "read_status"
    assert any(message.tool_call_id == "tool-1" for message in llm.requests[1].messages)


class _LegacyOpenAIAgent:
    name = "legacy_openai"

    def __init__(self, scm, state_manager, config, event_bus) -> None:  # noqa: ANN001
        from devai.providers.openai_provider import OpenAIProvider

        self.provider = OpenAIProvider(config)

    async def run(self, state):  # noqa: ANN001
        await asyncio.sleep(0)
        return {"text": await self.provider.generate("identify caller")}


@pytest.mark.asyncio
async def test_concurrent_legacy_runs_keep_user_llm_adapters_isolated():
    from devai.config import Settings

    config = Settings(llm_provider="vertex_gemini", openai_api_key="")
    first_llm = FakeLLMAdapter([LLMResponse(text="tenant-a")])
    second_llm = FakeLLMAdapter([LLMResponse(text="tenant-b")])
    first = LegacyAgent.from_class(_LegacyOpenAIAgent, require_deps=False)
    second = LegacyAgent.from_class(_LegacyOpenAIAgent, require_deps=False)

    first_result, second_result = await asyncio.gather(
        first.run(RunContext(task=_task(), deps=_deps(), llm=first_llm, config=config)),
        second.run(RunContext(task=_task(), deps=_deps(), llm=second_llm, config=config)),
    )

    assert first_result.handover["text"] == "tenant-a"
    assert second_result.handover["text"] == "tenant-b"
    assert len(first_llm.requests) == 1
    assert len(second_llm.requests) == 1


# ─── SpecAgent ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_spec_agent_runs_yaml_tool_loop():
    from devai.specializations import AgentRuntime

    spec = Specialization(
        name="summarizer",
        runtime=AgentRuntime.TESSERIX_ADK,
        handover_schema={"summary": HandoverField(name="summary", type="string", required=True)},
    )
    llm = FakeLLMAdapter([LLMResponse(text='```json\n{"summary": "did it"}\n```', usage=LLMUsage(prompt_tokens=7))])
    spec_agent = SpecAgent(spec)

    result = await spec_agent.run(RunContext(task=_task(), deps=_deps(llm)))

    assert result.ok is True
    assert result.stub is False
    assert result.handover == {"summary": "did it"}
    assert result.output_key == "summarizer_output"
    assert result.turns == 1
    assert result.prompt_tokens == 7
    assert result.trace_steps[0]["runtime"] == "tesserix-adk"
    assert llm.requests[0].response_format == {"type": "json_object"}


@pytest.mark.asyncio
async def test_spec_agent_routes_legacy_class_through_legacy_agent():
    # A spec that bridges to a (here, unimportable) Python class must route via
    # LegacyAgent and degrade to a stub — proving the routing, not the class.
    spec = Specialization(name="bridged", legacy_python_class="devai.nope.Missing")
    result = await SpecAgent(spec).run(RunContext(task=_task(), deps=_deps()))
    assert result.stub is True
    assert result.ok is False


@pytest.mark.asyncio
async def test_spec_agent_routes_migrated_legacy_definition_through_tesserix_adk():
    from devai.specializations import AgentRuntime

    spec = Specialization(
        name="bridged",
        legacy_python_class="devai.nope.Missing",
        runtime=AgentRuntime.TESSERIX_ADK,
        handover_schema={"summary": HandoverField(name="summary", type="string")},
    )
    llm = FakeLLMAdapter([LLMResponse(text='{"summary":"migrated"}')])

    result = await SpecAgent(spec).run(RunContext(task=_task(), deps=_deps(llm)))

    assert result.ok is True
    assert result.handover == {"summary": "migrated"}
    assert result.trace_steps[0]["runtime"] == "tesserix-adk"


@pytest.mark.asyncio
async def test_spec_agent_high_risk_parks_for_approval():
    from devai.pipeline.types import TaskState
    from devai.specializations.base import RiskLevel

    spec = Specialization(name="deployer", risk_level=RiskLevel.HIGH)
    llm = FakeLLMAdapter([LLMResponse(text='```json\n{"ok": true}\n```')])
    result = await SpecAgent(spec).run(RunContext(task=_task(), deps=_deps(llm)))
    assert result.next_state == TaskState.AWAITING_APPROVAL


# ─── AgentDispatcher (ADK) ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatcher_builds_context_and_runs_inline():
    llm = FakeLLMAdapter([LLMResponse(text='```json\n{"summary": "ok"}\n```')])
    dispatcher = AgentDispatcher(_deps(llm))
    spec = Specialization(
        name="summarizer",
        handover_schema={"summary": HandoverField(name="summary", type="string", required=True)},
    )

    result = await dispatcher.dispatch(SpecAgent(spec), _task())

    assert isinstance(dispatcher.backend, InlineBackend)
    assert result.handover == {"summary": "ok"}


@pytest.mark.asyncio
async def test_dispatch_many_fans_out_in_order():
    dispatcher = AgentDispatcher(_deps())
    agents = [FakeAgent("a"), FakeAgent("b"), FakeAgent("c")]

    results = await dispatcher.dispatch_many(list(agents), _task(intent="parallel"))

    assert [r.handover["who"] for r in results] == ["a", "b", "c"]
    assert all(r.handover["intent"] == "parallel" for r in results)


@pytest.mark.asyncio
async def test_dispatch_many_isolates_a_failing_agent():
    class Boom:
        name = "boom"

        async def run(self, ctx):  # noqa: ANN001
            raise RuntimeError("kaboom")

    dispatcher = AgentDispatcher(_deps())
    results = await dispatcher.dispatch_many([FakeAgent("ok"), Boom()], _task())

    assert results[0].handover["who"] == "ok"
    assert results[1].ok is False
    assert "kaboom" in results[1].error


@pytest.mark.asyncio
async def test_ctx_spawn_recurses_into_subagent():
    """The ROMA / RecursiveMAS primitive: an agent decomposes by spawning."""

    class Child:
        name = "child"

        async def run(self, ctx: RunContext) -> AgentResult:
            return AgentResult(handover={"child": "ran"})

    class Parent:
        name = "parent"

        async def run(self, ctx: RunContext) -> AgentResult:
            sub = await ctx.spawn(Child())
            return AgentResult(handover={"got": sub.handover["child"]})

    dispatcher = AgentDispatcher(_deps())
    result = await dispatcher.dispatch(Parent(), _task())
    assert result.handover == {"got": "ran"}


def test_concrete_agents_satisfy_the_protocol():
    assert isinstance(LegacyAgent(FakeBaseAgent()), Agent)
    assert isinstance(SpecAgent(Specialization(name="x")), Agent)
    assert isinstance(FakeAgent("n"), Agent)
