"""Card trust checks for the A2A consumer.

Two independent guards run before a pod ever calls a peer agent:

  1. ``verify_card_signature`` — the card's service ``url`` is attacker-
     influencable data. The registry signs each artifact's content digest with
     Ed25519 and publishes the public key; this verifies that signature over the
     digest carried in the card's provenance extension. A tampered card (e.g.
     one that swapped ``url`` to redirect agent traffic) fails verification.

  2. ``check_service_url`` — an SSRF allowlist. Even a validly signed card only
     gets called if its host is in the allowed-suffix list and is not a
     loopback / link-local / private IP literal (blocks metadata-endpoint and
     internal-service pivots).

Both raise :class:`A2AError` on rejection so the caller fails closed.
"""

from __future__ import annotations

import base64
import ipaddress
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from devai.a2a.errors import A2AError

if TYPE_CHECKING:
    from devai.a2a.card import AgentCard


def verify_card_signature(card: AgentCard, signing_key: dict[str, Any] | None) -> None:
    """Verify the registry's Ed25519 attestation over the card's digest.

    Raises A2AError when the registry isn't signing, the card carries no
    signature/digest, the signing key is unknown, or the signature is invalid.
    """
    if not signing_key or not signing_key.get("enabled"):
        raise A2AError(
            "a2a: registry signing is disabled — cannot verify card authenticity. "
            "Enable registry signing, or set DEVAI_A2A_SECURE=false for trusted local/dev only."
        )

    prov = card.provenance
    sig_b64 = prov.get("signature")
    digest = prov.get("digest")
    if not sig_b64 or not digest:
        raise A2AError(
            f"a2a: card for {card.name!r} carries no registry signature/digest; refusing to trust it"
        )

    pub_b64 = signing_key.get("publicKey")
    if not pub_b64:
        raise A2AError("a2a: registry signing key response has no publicKey")

    # When both sides advertise a key id, they must match — a card signed by a
    # key this registry doesn't publish is not trustworthy.
    signed_by, key_id = prov.get("signedBy"), signing_key.get("keyId")
    if signed_by and key_id and signed_by != key_id:
        raise A2AError(
            f"a2a: card {card.name!r} signed by key {signed_by!r}, but the registry publishes {key_id!r}"
        )

    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as e:  # pragma: no cover
        raise A2AError(f"a2a: cryptography not installed, cannot verify card: {e}") from e

    try:
        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(pub_b64))
        # The registry signs the digest STRING (e.g. "sha256:<hex>") verbatim.
        pub.verify(base64.b64decode(sig_b64), digest.encode("utf-8"))
    except InvalidSignature as e:
        raise A2AError(
            f"a2a: card signature for {card.name!r} is INVALID — possible tampering, refusing to call it"
        ) from e
    except Exception as e:  # noqa: BLE001 — malformed key/sig bytes
        raise A2AError(f"a2a: card signature check failed for {card.name!r}: {e}") from e


def check_service_url(url: str, allowed_suffixes: list[str]) -> None:
    """SSRF guard for a card's service URL. Raises A2AError when rejected.

    A host suffix of ``"*"`` disables the allowlist (still blocks private/
    link-local IP literals). Otherwise the host must end with an allowed suffix.
    """
    if not url:
        raise A2AError("a2a: card has no service url to call")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise A2AError(f"a2a: unsupported url scheme {parsed.scheme!r} in {url!r} (need http/https)")
    host = parsed.hostname
    if not host:
        raise A2AError(f"a2a: card url has no host: {url!r}")

    # Block SSRF-prone IP literals regardless of the allowlist.
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None and (
        ip.is_loopback
        or ip.is_link_local
        or ip.is_private
        or ip.is_unspecified
        or ip.is_reserved
        or ip.is_multicast
    ):
        raise A2AError(f"a2a: refusing to call non-routable address {host} (SSRF guard)")

    if "*" in allowed_suffixes:
        return
    host_l = host.lower()
    for suffix in allowed_suffixes:
        s = suffix.lower()
        if host_l == s or host_l == s.lstrip(".") or host_l.endswith(s):
            return
    raise A2AError(
        f"a2a: agent url host {host!r} is not in the allowed suffix list {allowed_suffixes}; "
        "add it to DEVAI_A2A_ALLOWED_URL_SUFFIXES to permit, or set DEVAI_A2A_SECURE=false for local dev"
    )
