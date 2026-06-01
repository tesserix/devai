"""Tests for GitLab webhook token verification (constant-time + non-bypassable)."""

from devai.scm.gitlab_client import GitLabSCMClient


def _client() -> GitLabSCMClient:
    return GitLabSCMClient(base_url="https://gitlab.com", token="t")


def test_matching_token_accepted():
    c = _client()
    assert c.verify_webhook_signature(b"{}", "shared-secret", "shared-secret")


def test_mismatched_token_rejected():
    c = _client()
    assert not c.verify_webhook_signature(b"{}", "wrong", "shared-secret")


def test_empty_secret_rejected():
    # A blank configured secret must never accept a blank header.
    c = _client()
    assert not c.verify_webhook_signature(b"{}", "", "")


def test_empty_signature_rejected():
    c = _client()
    assert not c.verify_webhook_signature(b"{}", "", "shared-secret")
