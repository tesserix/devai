from __future__ import annotations

from types import SimpleNamespace

import pytest

from devai.adapters.base import AdapterNotConfigured
from devai.adapters.llm.gateway_routing import gateway_base_url, gateway_headers


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
