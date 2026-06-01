"""ADO webhook verification must fail closed when secret/signature is absent."""

import hashlib
import hmac

from devai.scm.ado_client import AzureDevOpsSCMClient


def _client() -> AzureDevOpsSCMClient:
    return AzureDevOpsSCMClient(base_url="https://dev.azure.com", token="t", organization="org")


def test_valid_hmac_accepted():
    c = _client()
    body = b'{"eventType":"workitem.created"}'
    sig = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert c.verify_webhook_signature(body, sig, "secret")


def test_missing_secret_rejected():
    # Previously returned True (accept-all) — must now fail closed.
    c = _client()
    assert not c.verify_webhook_signature(b"{}", "anything", "")


def test_missing_signature_rejected():
    c = _client()
    assert not c.verify_webhook_signature(b"{}", "", "secret")


def test_wrong_signature_rejected():
    c = _client()
    assert not c.verify_webhook_signature(b"{}", "deadbeef", "secret")
