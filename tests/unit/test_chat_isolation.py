from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from langchain_core.messages import AIMessage, HumanMessage

from devai.chat.agent import DevAIChatAgent
from devai.identity import Principal

if TYPE_CHECKING:
    import pytest


class _Redis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: str, **_kwargs: Any) -> None:
        self.values[key] = value

    async def delete(self, key: str) -> None:
        self.values.pop(key, None)


class _BoundLLM:
    def __init__(self, llm: _LLM) -> None:
        self.llm = llm

    async def ainvoke(self, history: list[Any]) -> AIMessage:
        self.llm.histories.append(tuple(message.content for message in history if isinstance(message, HumanMessage)))
        self.llm.arrivals += 1
        if self.llm.arrivals == 2:
            self.llm.ready.set()
        await self.llm.ready.wait()
        return AIMessage(content="ok")


class _LLM:
    def __init__(self) -> None:
        self.histories: list[tuple[str, ...]] = []
        self.bind_kwargs: list[dict[str, Any]] = []
        self.arrivals = 0
        self.ready = asyncio.Event()

    def bind_tools(self, _tools: list[Any], **kwargs: Any) -> _BoundLLM:
        self.bind_kwargs.append(kwargs)
        return _BoundLLM(self)


def _agent(monkeypatch: pytest.MonkeyPatch, llm: _LLM | None = None) -> tuple[DevAIChatAgent, _LLM, _Redis]:
    llm = llm or _LLM()
    redis = _Redis()
    monkeypatch.setattr("devai.chat.agent.ChatAnthropic", lambda **_kwargs: llm)
    monkeypatch.setattr(DevAIChatAgent, "_build_tools", lambda _self: [])
    config = SimpleNamespace(
        claude_model="test-model",
        anthropic_api_key="test-key",
        anthropic_base_url="https://api.anthropic.com",
        llm_gateway_base_url="http://agent-gateway:8080",
        llm_gateway_required=True,
    )
    return DevAIChatAgent(config, SimpleNamespace(redis=redis)), llm, redis


async def test_same_session_id_does_not_mix_concurrent_user_histories(monkeypatch: pytest.MonkeyPatch) -> None:
    agent, llm, redis = _agent(monkeypatch)
    alice = Principal(email="alice@example.com", uid="shared-id", tenant_id="tenant-a")
    bob = Principal(email="bob@example.com", uid="shared-id", tenant_id="tenant-b")

    await asyncio.gather(
        agent.chat("alice message", "shared-session", principal=alice, trace_id="trace-a"),
        agent.chat("bob message", "shared-session", principal=bob, trace_id="trace-b"),
    )

    assert set(llm.histories) == {("alice message",), ("bob message",)}
    assert len(redis.values) == 2
    assert all("alice" not in key and "bob" not in key for key in redis.values)


async def test_concurrent_chat_gateway_requests_carry_their_own_principal(monkeypatch: pytest.MonkeyPatch) -> None:
    agent, llm, _redis = _agent(monkeypatch)
    alice = Principal(email="alice@example.com", uid="alice", tenant_id="tenant-a")
    bob = Principal(email="bob@example.com", uid="bob", tenant_id="tenant-b")

    await asyncio.gather(
        agent.chat("alice message", "one", principal=alice, trace_id="trace-a"),
        agent.chat("bob message", "two", principal=bob, trace_id="trace-b"),
    )

    assert {tuple(sorted(call["extra_headers"].items())) for call in llm.bind_kwargs} == {
        tuple(
            sorted(
                {
                    "x-devai-tenant-id": "tenant-a",
                    "x-devai-user-id": "alice",
                    "x-devai-run-id": "trace-a",
                    "x-devai-agent": "chat",
                    "x-devai-provider": "anthropic",
                }.items()
            )
        ),
        tuple(
            sorted(
                {
                    "x-devai-tenant-id": "tenant-b",
                    "x-devai-user-id": "bob",
                    "x-devai-run-id": "trace-b",
                    "x-devai-agent": "chat",
                    "x-devai-provider": "anthropic",
                }.items()
            )
        ),
    }


class _ToolBoundLLM:
    def __init__(self, llm: _ToolLLM) -> None:
        self.llm = llm
        self.calls = 0

    async def ainvoke(self, _history: list[Any]) -> AIMessage:
        self.calls += 1
        if self.calls > 1:
            return AIMessage(content="ok")
        self.llm.arrivals += 1
        if self.llm.arrivals == 2:
            self.llm.ready.set()
        await self.llm.ready.wait()
        return AIMessage(
            content="",
            tool_calls=[{"name": "capture_principal", "args": {}, "id": f"tool-{self.llm.arrivals}"}],
        )


class _ToolLLM(_LLM):
    def bind_tools(self, _tools: list[Any], **kwargs: Any) -> _ToolBoundLLM:
        self.bind_kwargs.append(kwargs)
        return _ToolBoundLLM(self)


class _PrincipalTool:
    name = "capture_principal"

    def __init__(self, agent: DevAIChatAgent) -> None:
        self.agent = agent
        self.seen: list[str] = []

    async def ainvoke(self, _args: dict[str, Any]) -> str:
        principal = self.agent._principal
        self.seen.append(principal.email if principal else "")
        await asyncio.sleep(0)
        return "captured"


async def test_concurrent_chat_tools_use_task_local_principal(monkeypatch: pytest.MonkeyPatch) -> None:
    agent, _llm, _redis = _agent(monkeypatch, _ToolLLM())
    tool = _PrincipalTool(agent)
    agent._tools = [tool]
    alice = Principal(email="alice@example.com", uid="alice", tenant_id="tenant-a")
    bob = Principal(email="bob@example.com", uid="bob", tenant_id="tenant-b")

    await asyncio.gather(
        agent.chat("alice message", "one", principal=alice, trace_id="trace-a"),
        agent.chat("bob message", "two", principal=bob, trace_id="trace-b"),
    )

    assert set(tool.seen) == {"alice@example.com", "bob@example.com"}
