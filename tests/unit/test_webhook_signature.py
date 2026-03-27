"""Tests for webhook signature verification."""

import hashlib
import hmac

from devai.webhook.signature import verify_github_signature


def test_valid_signature():
    secret = "test-secret"
    payload = b'{"action": "labeled"}'
    sig = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    assert verify_github_signature(payload, sig, secret)


def test_invalid_signature():
    assert not verify_github_signature(b"payload", "sha256=invalid", "secret")


def test_missing_signature():
    assert not verify_github_signature(b"payload", None, "secret")


def test_empty_secret():
    assert not verify_github_signature(b"payload", "sha256=abc", "")


def test_wrong_prefix():
    assert not verify_github_signature(b"payload", "sha1=abc", "secret")
