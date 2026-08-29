from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from anthropic.types import TextBlock

from devai.providers.anthropic_claude import ClaudeProvider
from devai.providers.gemini_provider import GeminiProvider
from devai.providers.groq_provider import GroqProvider
from devai.providers.nemoclaw_provider import NemoClawProvider
from devai.providers.openai_codex import CodexLiteProvider, CodexSandboxProvider
from devai.providers.openai_provider import OpenAIProvider
from devai.services.agent_turns import reset_turn_context, set_turn_context, update_turn_context

EXPECTED_HEADERS = {
    "x-devai-tenant-id": "tenant-a",
    "x-devai-user-id": "user-a",
    "x-devai-run-id": "run-a",
    "x-devai-agent": "developer",
}


class _Create:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.response


class _Client:
    def __init__(self, response: Any) -> None:
        create = _Create(response)
        self.create = create
        self.chat = SimpleNamespace(completions=create)
        self.responses = create
        self.messages = create


@pytest.fixture
def turn_context() -> None:
    token = set_turn_context("run-a", "developer", "implement")
    update_turn_context(tenant_id="tenant-a", user_id="user-a")
    try:
        yield
    finally:
        reset_turn_context(token)


@pytest.mark.parametrize(
    ("provider_name", "provider", "method", "client_attr"),
    [
        (
            "openai",
            OpenAIProvider,
            lambda instance: instance.generate("hello"),
            "client",
        ),
        (
            "groq",
            GroqProvider,
            lambda instance: instance.generate("hello"),
            "client",
        ),
        (
            "gemini",
            GeminiProvider,
            lambda instance: instance.generate("hello"),
            "_gateway_client",
        ),
        (
            "openai",
            CodexLiteProvider,
            lambda instance: instance.generate("hello", "system"),
            "client",
        ),
        (
            "nemoclaw",
            NemoClawProvider,
            lambda instance: instance.generate("hello"),
            "client",
        ),
    ],
)
async def test_openai_compatible_legacy_provider_attributes_each_gateway_request(
    provider_name: str,
    provider: type[Any],
    method: Any,
    client_attr: str,
    turn_context: None,
) -> None:
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
        output_text="ok",
    )
    client = _Client(response)
    instance = object.__new__(provider)
    instance._gateway_required = True
    instance._config = SimpleNamespace(overlaid_attrs=(), anthropic_api_key="", openai_api_key="")
    instance.model = "test-model"
    instance._model_name = "test-model"
    instance._client = None
    instance._fallback_enabled = False
    instance.max_tokens = 100
    setattr(instance, client_attr, client)

    assert await method(instance) == "ok"
    assert client.create.calls[0]["extra_headers"] == {
        **EXPECTED_HEADERS,
        "x-devai-provider": provider_name,
    }


async def test_claude_attributes_one_shot_gateway_request(monkeypatch: pytest.MonkeyPatch, turn_context: None) -> None:
    class _Limiter:
        async def acquire(self, _tokens: int) -> None:
            return None

    monkeypatch.setattr("devai.providers.anthropic_claude.get_claude_rate_limiter", lambda: _Limiter())
    client = _Client(SimpleNamespace(content=[TextBlock(text="ok", type="text")]))
    provider = object.__new__(ClaudeProvider)
    provider._gateway_required = True
    provider.client = client
    provider.model = "test-model"
    provider.max_tokens = 100

    assert await provider.generate("system", "hello") == "ok"
    assert client.create.calls[0]["extra_headers"] == {
        **EXPECTED_HEADERS,
        "x-devai-provider": "anthropic",
    }


async def test_codex_sandbox_uses_request_scoped_gateway_header_environment(
    monkeypatch: pytest.MonkeyPatch,
    turn_context: None,
) -> None:
    captured: dict[str, Any] = {}

    def fake_run(command: list[str], **kwargs: Any) -> SimpleNamespace:
        if command[0] == "codex":
            environment = kwargs["env"]
            captured["environment"] = environment
            captured["config"] = (Path(environment["CODEX_HOME"]) / "config.toml").read_text()
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr("devai.providers.openai_codex.subprocess.run", fake_run)
    provider = CodexSandboxProvider(
        SimpleNamespace(
            openai_api_key="provider-secret",
            openai_base_url="https://api.openai.com/v1",
            openai_model="gpt-test",
            llm_gateway_required=True,
            llm_gateway_base_url="http://agent-gateway:8080",
        )
    )

    result = await provider.run_in_sandbox("review", "https://example.invalid/repo.git", "main")

    assert result == {"success": True, "output": "ok", "error": ""}
    assert "provider-secret" not in captured["config"]
    assert 'model_provider = "devai_gateway"' in captured["config"]
    assert 'base_url = "http://agent-gateway:8080/openai/v1"' in captured["config"]
    assert '"x-devai-tenant-id" = "DEVAI_GATEWAY_TENANT_ID"' in captured["config"]
    assert '"x-devai-user-id" = "DEVAI_GATEWAY_USER_ID"' in captured["config"]
    assert '"x-devai-run-id" = "DEVAI_GATEWAY_RUN_ID"' in captured["config"]
    assert '"x-devai-agent" = "DEVAI_GATEWAY_AGENT"' in captured["config"]
    expected_environment = {
        "DEVAI_GATEWAY_TENANT_ID": "tenant-a",
        "DEVAI_GATEWAY_USER_ID": "user-a",
        "DEVAI_GATEWAY_RUN_ID": "run-a",
        "DEVAI_GATEWAY_AGENT": "developer",
        "DEVAI_GATEWAY_PROVIDER": "openai",
    }
    assert {name: captured["environment"][name] for name in expected_environment} == expected_environment
