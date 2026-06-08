"""Tests for credential redaction in logs / API responses."""

import pytest

from devai.services.redact import redact_secrets


def test_redacts_github_clone_credentials():
    line = (
        "fatal: unable to access "
        "'https://x-access-token:ghp_AbCdEf123456789@github.com/acme/app.git/': "
        "The requested URL returned error: 404"
    )
    out = redact_secrets(line)
    assert "ghp_AbCdEf123456789" not in out
    assert "x-access-token:***@github.com" in out


def test_redacts_gitlab_oauth_credentials():
    line = "remote: https://oauth2:glpat-SECRETtoken99@gitlab.com/acme/app.git"
    out = redact_secrets(line)
    assert "glpat-SECRETtoken99" not in out
    assert "oauth2:***@gitlab.com" in out


def test_redacts_bearer_token():
    out = redact_secrets("Authorization: Bearer sk-1234567890abcdef")
    assert "sk-1234567890abcdef" not in out
    assert "Bearer ***" in out


def test_noop_on_clean_text():
    clean = "fatal: repository not found"
    assert redact_secrets(clean) == clean


def test_empty_input():
    assert redact_secrets("") == ""


@pytest.mark.parametrize(
    ("secret", "kept_prefix"),
    [
        ("sk-ant-api03-AbCdEf01234567890XyZ", "sk-ant-"),
        ("sk-proj-AbCdEf01234567890XyZ", "sk-"),
        ("ghp_AbCdEf012345678901234567890123456789", "ghp_"),
        ("gho_AbCdEf012345678901234567890123456789", "gho_"),
        ("ghu_AbCdEf012345678901234567890123456789", "ghu_"),
        ("ghs_AbCdEf012345678901234567890123456789", "ghs_"),
        ("lsv2_pt_AbCdEf01234567890_abc123def456", "lsv2_pt_"),
        ("xoxb-1234567890-ABCDEFghijkl", "xoxb-"),
        ("AKIAIOSFODNN7EXAMPLE", ""),
        ("glpat-AbCdEf01234567890Xy", "glpat-"),
    ],
)
def test_redacts_bare_provider_keys(secret, kept_prefix):
    """Each provider-key prefix is masked, keeping only the prefix marker."""
    line = f"error calling api with key {secret} — failed"
    out = redact_secrets(line)
    assert secret not in out
    if kept_prefix:
        assert f"{kept_prefix}***" in out
    else:
        assert "***" in out


@pytest.mark.parametrize(
    "fragment",
    [
        "password=SuperSecret123",
        "api_key: abc123def456",
        "api-key=abc123def456",
        'secret="topsecretvalue"',
        "access_token=tok_abcdef123456",
    ],
)
def test_redacts_secret_field_fragments(fragment):
    """``field=value`` style secret fragments mask the value, keep the field."""
    out = redact_secrets(f"config dump: {fragment}")
    field = fragment.split("=")[0].split(":")[0].strip()
    assert field in out
    assert "***" in out
    # The original value must not survive.
    raw_value = fragment.split("=", 1)[-1].split(":", 1)[-1].strip().strip("\"'")
    assert raw_value not in out


def test_redacts_anthropic_key_before_short_sk():
    """sk-ant- keys keep their full prefix, not just the sk- prefix."""
    out = redact_secrets("ANTHROPIC_API_KEY=sk-ant-api03-LEAKEDvalue0123456789")
    assert "LEAKEDvalue" not in out
    assert "sk-ant-***" in out
