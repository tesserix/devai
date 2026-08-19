"""Fail-closed provider routing and sanitized AgentGateway attribution."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from devai.adapters.base import AdapterNotConfigured

_ROUTES = {
    "anthropic": "anthropic",
    "openai": "openai/v1",
    "groq": "groq/openai/v1",
    "openrouter": "openrouter/api/v1",
    "vertex_gemini": "vertex",
    "gemini": "gemini/v1",
    "nemoclaw": "nemoclaw/v1",
    "gateway": "openai/v1",
}


def gateway_required(settings: Any) -> bool:
    return bool(getattr(settings, "llm_gateway_required", False))


def gateway_base_url(settings: Any, provider: str, direct_url: str = "") -> str:
    if not gateway_required(settings) and provider != "gateway":
        return direct_url
    origin = str(getattr(settings, "llm_gateway_base_url", "") or "").rstrip("/")
    route = _ROUTES.get(provider)
    if not origin or not route:
        raise AdapterNotConfigured(f"{provider} requires a configured AgentGateway route (DEVAI_LLM_GATEWAY_BASE_URL)")
    return f"{origin}/{route}"


def _safe(value: Any) -> str:
    return str(value or "").replace("\r", "").replace("\n", "")[:256]


def gateway_headers(extra: Mapping[str, Any] | None, *, provider: str) -> dict[str, str]:
    metadata = extra or {}
    headers = {
        "x-devai-tenant-id": _safe(metadata.get("tenant_id")),
        "x-devai-user-id": _safe(metadata.get("user_id")),
        "x-devai-run-id": _safe(metadata.get("run_id")),
        "x-devai-agent": _safe(metadata.get("agent")),
        "x-devai-provider": _safe(provider),
    }
    return {name: value for name, value in headers.items() if value}


def current_gateway_headers(provider: str) -> dict[str, str]:
    from devai.services.agent_turns import get_turn_context

    return gateway_headers(get_turn_context(), provider=provider)


__all__ = ["current_gateway_headers", "gateway_base_url", "gateway_headers", "gateway_required"]
