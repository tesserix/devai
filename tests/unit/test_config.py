"""Tests for configuration."""

import os

import pytest

from devai.config import Settings


def test_default_settings():
    s = Settings(
        _env_file=None,  # Don't load .env for tests
    )
    assert s.nats_url == "nats://localhost:4222"
    assert s.redis_url == "redis://localhost:6379"
    assert s.nats_stream == "DEVAI"
    assert s.claude_model == "claude-sonnet-4-20250514"
    assert s.openai_model == "gpt-4.1"
    assert s.max_review_iterations == 3
    assert s.pipeline_label == "devai:automate"
    assert s.port == 8080


def test_github_app_not_configured():
    s = Settings(_env_file=None)
    assert not s.is_github_app_configured


def test_github_app_configured():
    s = Settings(
        _env_file=None,
        github_app_id=12345,
        github_app_private_key="-----BEGIN RSA PRIVATE KEY-----\ntest\n-----END RSA PRIVATE KEY-----",
    )
    assert s.is_github_app_configured


@pytest.fixture
def _clean_langsmith_env():
    saved = {k: os.environ.get(k) for k in ("LANGCHAIN_API_KEY", "LANGCHAIN_TRACING_V2", "LANGCHAIN_PROJECT")}
    for k in saved:
        os.environ.pop(k, None)
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def test_a_placeholder_key_does_not_turn_tracing_on(_clean_langsmith_env):
    """A secret left at its bootstrap placeholder made every trace export 403,
    once per LLM call, with nothing in LangSmith to show for it."""
    s = Settings(_env_file=None, langchain_api_key="PLACEHOLDER_REPLACE_ME", langchain_tracing_v2="true")
    s.export_langsmith_env()
    assert "LANGCHAIN_TRACING_V2" not in os.environ
    assert "LANGCHAIN_API_KEY" not in os.environ


@pytest.mark.parametrize("key", ["lsv2_pt_deadbeef", "lsv2_sk_deadbeef", "ls__deadbeef"])
def test_a_real_key_turns_tracing_on(key, _clean_langsmith_env):
    s = Settings(_env_file=None, langchain_api_key=key, langchain_tracing_v2="true")
    s.export_langsmith_env()
    assert os.environ["LANGCHAIN_TRACING_V2"] == "true"
    assert os.environ["LANGCHAIN_API_KEY"] == key
