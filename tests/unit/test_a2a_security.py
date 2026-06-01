"""A2A consumer trust guards: registry signature verification + SSRF allowlist."""

from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from devai.a2a import A2AClient, A2AError, AgentCard, check_service_url, verify_card_signature

_DIGEST = "sha256:deadbeefcafe"


def _keypair() -> tuple[Ed25519PrivateKey, str]:
    priv = Ed25519PrivateKey.generate()
    pub_raw = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return priv, base64.b64encode(pub_raw).decode()


def _sign(priv: Ed25519PrivateKey, message: str) -> str:
    return base64.b64encode(priv.sign(message.encode())).decode()


def _signing_key(pub_b64: str, *, enabled: bool = True) -> dict:
    return {"enabled": enabled, "algorithm": "ed25519", "keyId": "k1", "publicKey": pub_b64, "signs": "digest"}


def _signed_card(priv: Ed25519PrivateKey, *, url: str = "https://oncall.sre.svc.cluster.local/a2a/v1", digest: str = _DIGEST) -> AgentCard:
    return AgentCard.from_dict(
        {
            "name": "oncall",
            "url": url,
            "capabilities": {
                "extensions": [
                    {
                        "uri": "https://registry.agentic.dev/ext/provenance",
                        "params": {"digest": digest, "signature": _sign(priv, digest)},
                    }
                ]
            },
        }
    )


class SigningRegistry:
    """Fake registry that serves a fixed signing key + the agent's signed card."""

    def __init__(self, signing_key: dict, card: AgentCard) -> None:
        self._key = signing_key
        self._card = card

    def get_signing_key(self) -> dict:
        return self._key

    def get_agent_card(self, name, *, namespace="", tag=""):
        return self._card.raw

    def list_agents(self):
        return []


# --------------------------------------------------------------------- #
# Signature verification
# --------------------------------------------------------------------- #


def test_verify_signature_accepts_valid_card() -> None:
    priv, pub = _keypair()
    verify_card_signature(_signed_card(priv), _signing_key(pub))  # no raise


def test_verify_signature_rejects_tampered_signature() -> None:
    priv, pub = _keypair()
    other_priv, _ = _keypair()
    card = _signed_card(priv)
    # Re-sign with a different key => signature no longer matches the published key.
    card.capabilities["extensions"][0]["params"]["signature"] = _sign(other_priv, _DIGEST)
    with pytest.raises(A2AError, match="INVALID"):
        verify_card_signature(card, _signing_key(pub))


def test_verify_signature_rejects_when_registry_signing_disabled() -> None:
    priv, pub = _keypair()
    with pytest.raises(A2AError, match="signing is disabled"):
        verify_card_signature(_signed_card(priv), _signing_key(pub, enabled=False))


def test_verify_signature_rejects_unsigned_card() -> None:
    _, pub = _keypair()
    bare = AgentCard.from_dict({"name": "oncall", "url": "https://x.svc"})
    with pytest.raises(A2AError, match="no registry signature"):
        verify_card_signature(bare, _signing_key(pub))


# --------------------------------------------------------------------- #
# SSRF allowlist
# --------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "url",
    [
        "https://oncall.sre.svc.cluster.local/a2a/v1",
        "http://agent.team-a.svc/a2a",
    ],
)
def test_check_url_allows_cluster_internal(url: str) -> None:
    check_service_url(url, [".svc.cluster.local", ".svc"])  # no raise


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data",  # cloud metadata SSRF
        "http://127.0.0.1:8080/a2a",  # loopback
        "http://10.1.2.3/a2a",  # private
        "ftp://oncall.svc/a2a",  # bad scheme
        "https://evil.example.com/a2a",  # not in allowlist
    ],
)
def test_check_url_rejects_dangerous_targets(url: str) -> None:
    with pytest.raises(A2AError):
        check_service_url(url, [".svc.cluster.local", ".svc"])


def test_check_url_wildcard_allows_public_but_still_blocks_private_ip() -> None:
    check_service_url("https://evil.example.com/a2a", ["*"])  # wildcard permits hostnames
    with pytest.raises(A2AError, match="SSRF"):
        check_service_url("http://169.254.169.254/", ["*"])  # IP guard still applies


# --------------------------------------------------------------------- #
# End-to-end through the client (fail closed before any call)
# --------------------------------------------------------------------- #


def test_send_message_secure_verifies_then_calls() -> None:
    priv, pub = _keypair()
    card = _signed_card(priv)
    client = A2AClient(SigningRegistry(_signing_key(pub), card), verify_cards=True,
                       allowed_url_suffixes=[".svc.cluster.local"])
    called = {}
    client._rpc = lambda url, method, params: called.setdefault("url", url) or {"ok": True}  # type: ignore[method-assign]
    client.send_message(card, "hello")
    assert called["url"] == card.url


def test_send_message_secure_refuses_unsigned_card_before_calling() -> None:
    priv, pub = _keypair()
    unsigned = AgentCard.from_dict({"name": "oncall", "url": "https://oncall.svc.cluster.local/a2a"})
    client = A2AClient(SigningRegistry(_signing_key(pub), unsigned), verify_cards=True)

    def boom(*_a, **_k):
        raise AssertionError("must not call the agent when verification fails")

    client._rpc = boom  # type: ignore[method-assign]
    with pytest.raises(A2AError):
        client.send_message(unsigned, "hello")


def test_send_message_secure_refuses_disallowed_url_even_if_signed() -> None:
    priv, pub = _keypair()
    card = _signed_card(priv, url="https://evil.example.com/a2a")
    client = A2AClient(SigningRegistry(_signing_key(pub), card), verify_cards=True,
                       allowed_url_suffixes=[".svc.cluster.local"])
    client._rpc = lambda *a, **k: pytest.fail("must not call a disallowed url")  # type: ignore[method-assign]
    with pytest.raises(A2AError, match="allowed suffix"):
        client.send_message(card, "hello")
