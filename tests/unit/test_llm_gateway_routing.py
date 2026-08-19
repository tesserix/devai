from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from devai.adapters.base import AdapterNotConfigured
from devai.adapters.llm.gateway_routing import current_gateway_headers, gateway_base_url, gateway_headers
from devai.services.agent_turns import reset_turn_context, set_turn_context, update_turn_context


@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        ("anthropic", "http://ai-gateway.agentgateway-system.svc.cluster.local:8080/anthropic"),
        ("openai", "http://ai-gateway.agentgateway-system.svc.cluster.local:8080/openai/v1"),
        ("groq", "http://ai-gateway.agentgateway-system.svc.cluster.local:8080/groq/openai/v1"),
        ("openrouter", "http://ai-gateway.agentgateway-system.svc.cluster.local:8080/openrouter/api/v1"),
        ("vertex_gemini", "http://ai-gateway.agentgateway-system.svc.cluster.local:8080/vertex"),
        ("gemini", "http://ai-gateway.agentgateway-system.svc.cluster.local:8080/gemini/v1"),
        ("nemoclaw", "http://ai-gateway.agentgateway-system.svc.cluster.local:8080/nemoclaw/v1"),
        ("gateway", "http://ai-gateway.agentgateway-system.svc.cluster.local:8080/openai/v1"),
    ],
)
def test_required_gateway_maps_every_provider(provider: str, expected: str) -> None:
    settings = SimpleNamespace(
        llm_gateway_required=True,
        llm_gateway_base_url="http://ai-gateway.agentgateway-system.svc.cluster.local:8080/",
    )
    assert gateway_base_url(settings, provider, "https://direct.invalid") == expected


def test_required_gateway_fails_closed_without_origin() -> None:
    settings = SimpleNamespace(llm_gateway_required=True, llm_gateway_base_url="")
    with pytest.raises(AdapterNotConfigured):
        gateway_base_url(settings, "anthropic", "https://api.anthropic.com")


def test_optional_gateway_keeps_configured_direct_url() -> None:
    settings = SimpleNamespace(llm_gateway_required=False, llm_gateway_base_url="")
    assert gateway_base_url(settings, "groq", "https://api.groq.com/openai/v1") == ("https://api.groq.com/openai/v1")


def test_gateway_headers_are_sanitized_and_attributable() -> None:
    headers = gateway_headers(
        {
            "tenant_id": "tenant-a\r\ninjected: yes",
            "user_id": "user-a",
            "run_id": "run-a",
            "agent": "developer",
        },
        provider="anthropic",
    )
    assert headers == {
        "x-devai-tenant-id": "tenant-ainjected: yes",
        "x-devai-user-id": "user-a",
        "x-devai-run-id": "run-a",
        "x-devai-agent": "developer",
        "x-devai-provider": "anthropic",
    }


def test_current_gateway_headers_use_request_turn_context() -> None:
    token = set_turn_context("run-a", "developer", "implement")
    try:
        update_turn_context(tenant_id="tenant-a", user_id="user-a")

        assert current_gateway_headers("openai") == {
            "x-devai-tenant-id": "tenant-a",
            "x-devai-user-id": "user-a",
            "x-devai-run-id": "run-a",
            "x-devai-agent": "developer",
            "x-devai-provider": "openai",
        }
    finally:
        reset_turn_context(token)


async def test_current_gateway_headers_do_not_leak_between_concurrent_users() -> None:
    async def resolve(tenant_id: str, user_id: str, run_id: str) -> dict[str, str]:
        token = set_turn_context(run_id, "reviewer", "review")
        try:
            update_turn_context(tenant_id=tenant_id, user_id=user_id)
            await asyncio.sleep(0)
            return current_gateway_headers("anthropic")
        finally:
            reset_turn_context(token)

    alice, bob = await asyncio.gather(
        resolve("tenant-a", "alice", "run-a"),
        resolve("tenant-b", "bob", "run-b"),
    )

    assert alice["x-devai-tenant-id"] == "tenant-a"
    assert alice["x-devai-user-id"] == "alice"
    assert alice["x-devai-run-id"] == "run-a"
    assert bob["x-devai-tenant-id"] == "tenant-b"
    assert bob["x-devai-user-id"] == "bob"
    assert bob["x-devai-run-id"] == "run-b"
