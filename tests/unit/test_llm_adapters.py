"""LLM adapter family — interface contract + factory + dataclass round-trip.

Three layers of coverage matching the memory adapter tests:

1. **Contract** — every adapter we can instantiate in a slim env (Noop
   for now) passes the same set of behaviors: generate returns
   LLMResponse, embed shape correct, close idempotent.

2. **Factory** — every value of DEVAI_LLM_PROVIDER resolves to a usable
   adapter; failure modes degrade to Noop.

3. **Dataclass round-trip** — LLMRequest/LLMResponse/ToolCall serialize
   without losing fields.
"""

from __future__ import annotations

import pytest

from devai.adapters.base import AdapterError
from devai.adapters.llm import (
    KNOWN_PROVIDERS,
    LLMAdapter,
    LLMMessage,
    LLMRequest,
    LLMResponse,
    LLMRole,
    ToolCall,
    ToolSpec,
    create_llm_adapter,
    llm_registry,
)
from devai.adapters.llm.base import LLMUsage, make_request
from devai.adapters.llm.noop import NoopLLMAdapter


class _Settings:
    llm_provider = "noop"
    anthropic_api_key = ""
    openai_api_key = ""
    claude_model = ""
    openai_model = ""
    claude_max_tokens = 0


# ─────────────────────────────────────────────────────────────────────
# Contract — every adapter is interchangeable
# ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def noop_adapter() -> NoopLLMAdapter:
    return NoopLLMAdapter(canned_text="hello from noop")


@pytest.mark.asyncio
async def test_contract_generate_returns_llmresponse(noop_adapter):
    req = LLMRequest(system="be terse", messages=[LLMMessage(role=LLMRole.USER, content="hi")])
    resp = await noop_adapter.generate(req)
    assert isinstance(resp, LLMResponse)
    assert resp.provider == "noop"
    assert resp.text == "hello from noop"
    assert resp.finish_reason == "stop"


@pytest.mark.asyncio
async def test_contract_queued_responses_replay_in_order(noop_adapter):
    noop_adapter.set_responses(
        [
            LLMResponse(text="first"),
            LLMResponse(text="second"),
            LLMResponse(text="third"),
        ]
    )
    req = LLMRequest(messages=[LLMMessage(role=LLMRole.USER, content="x")])
    out = [(await noop_adapter.generate(req)).text for _ in range(3)]
    assert out == ["first", "second", "third"]
    # Queue empty → returns canned text
    assert (await noop_adapter.generate(req)).text == "hello from noop"


@pytest.mark.asyncio
async def test_contract_stream_yields_partials_then_full(noop_adapter):
    req = LLMRequest(messages=[LLMMessage(role=LLMRole.USER, content="stream please")])
    chunks: list[str] = []
    async for chunk in noop_adapter.stream(req):
        chunks.append(chunk.text)
    assert len(chunks) >= 1
    assert chunks[-1] == "hello from noop"


@pytest.mark.asyncio
async def test_contract_embed_shape(noop_adapter):
    out = await noop_adapter.embed(["alpha", "beta"])
    assert len(out) == 2
    assert all(isinstance(v, list) and len(v) == 4 for v in out)


@pytest.mark.asyncio
async def test_contract_close_is_idempotent(noop_adapter):
    await noop_adapter.close()
    await noop_adapter.close()


@pytest.mark.asyncio
async def test_contract_health_check_shape(noop_adapter):
    h = await noop_adapter.health_check()
    assert h["ok"] is True
    assert h["provider"] == "noop"


# ─────────────────────────────────────────────────────────────────────
# Tool-calling contract
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tool_call_round_trips(noop_adapter):
    noop_adapter.set_responses(
        [
            LLMResponse(
                text="",
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="get_weather",
                        arguments={"city": "Bangalore"},
                    )
                ],
                finish_reason="tool_use",
            )
        ]
    )
    req = LLMRequest(
        system="you have tools",
        messages=[LLMMessage(role=LLMRole.USER, content="weather please")],
        tools=[
            ToolSpec(
                name="get_weather",
                description="returns weather",
                parameters={"type": "object", "properties": {"city": {"type": "string"}}},
            )
        ],
    )
    resp = await noop_adapter.generate(req)
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].name == "get_weather"
    assert resp.tool_calls[0].arguments == {"city": "Bangalore"}


# ─────────────────────────────────────────────────────────────────────
# Factory — graceful degradation
# ─────────────────────────────────────────────────────────────────────


def test_registry_lists_every_known_provider():
    assert set(KNOWN_PROVIDERS) == set(llm_registry.known())


def test_factory_noop_default():
    s = _Settings()
    s.llm_provider = "noop"
    adapter = create_llm_adapter(s)
    assert adapter.provider_name == "noop"


def test_factory_unknown_falls_back_to_noop(caplog):
    s = _Settings()
    s.llm_provider = "no-such-provider"
    with caplog.at_level("WARNING"):
        a = create_llm_adapter(s)
    assert a.provider_name == "noop"
    assert any("unknown" in r.message.lower() for r in caplog.records)


def test_factory_anthropic_without_key_falls_back_to_noop():
    s = _Settings()
    s.llm_provider = "anthropic"
    a = create_llm_adapter(s)
    assert a.provider_name == "noop"


def test_factory_openai_without_key_falls_back_to_noop():
    s = _Settings()
    s.llm_provider = "openai"
    a = create_llm_adapter(s)
    assert a.provider_name == "noop"


def test_factory_explicit_provider_override_wins_over_settings():
    s = _Settings()
    s.llm_provider = "anthropic"  # would fail without key
    # Override to noop → factory uses that, ignores the broken default
    a = create_llm_adapter(s, provider="noop")
    assert a.provider_name == "noop"


# ─────────────────────────────────────────────────────────────────────
# Dataclass round-trip
# ─────────────────────────────────────────────────────────────────────


def test_message_to_dict_keeps_role_and_content():
    m = LLMMessage(role=LLMRole.ASSISTANT, content="x")
    assert m.to_dict() == {"role": "assistant", "content": "x"}


def test_message_with_tool_call_id_serializes():
    m = LLMMessage(role=LLMRole.TOOL, content='{"weather":"rainy"}', tool_call_id="call_1", name="get_weather")
    d = m.to_dict()
    assert d["role"] == "tool"
    assert d["tool_call_id"] == "call_1"
    assert d["name"] == "get_weather"


def test_response_to_dict_carries_usage_and_tool_calls():
    r = LLMResponse(
        text="ok",
        tool_calls=[ToolCall(id="c1", name="t", arguments={"a": 1})],
        usage=LLMUsage(prompt_tokens=10, completion_tokens=20, total_tokens=30),
        finish_reason="stop",
        model="claude-sonnet-4",
        provider="anthropic",
    )
    d = r.to_dict()
    assert d["text"] == "ok"
    assert d["tool_calls"][0]["name"] == "t"
    assert d["usage"]["total_tokens"] == 30
    assert d["model"] == "claude-sonnet-4"


def test_make_request_helper_composes_messages():
    req = make_request(system="be brief", user="hi")
    assert req.system == "be brief"
    assert len(req.messages) == 1
    assert req.messages[0].role == LLMRole.USER
    assert req.messages[0].content == "hi"


def test_role_parse_accepts_string_or_enum():
    assert LLMRole.parse("system") == LLMRole.SYSTEM
    assert LLMRole.parse(LLMRole.USER) == LLMRole.USER
    assert LLMRole.parse(None, default=LLMRole.ASSISTANT) == LLMRole.ASSISTANT
    # Unknown → default
    assert LLMRole.parse("invalid", default=LLMRole.SYSTEM) == LLMRole.SYSTEM


# ─────────────────────────────────────────────────────────────────────
# StageDeps integration
# ─────────────────────────────────────────────────────────────────────


def test_stagedeps_carries_llm_adapter_field():
    from devai.pipeline.interfaces import StageDeps

    class _Cfg:
        pipeline_label = "x"

    adapter = NoopLLMAdapter()
    deps = StageDeps(config=_Cfg(), llm=adapter)
    assert deps.llm is adapter
    # And it's None when not provided — stages must tolerate
    no_llm = StageDeps(config=_Cfg())
    assert no_llm.llm is None
