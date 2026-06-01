"""Tests for credential redaction in logs / API responses."""

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
