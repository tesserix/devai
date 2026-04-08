"""Tests for configuration."""

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
