"""GitHub webhook HMAC signature verification."""

from __future__ import annotations

import hashlib
import hmac


def verify_github_signature(payload: bytes, signature: str | None, secret: str) -> bool:
    """Verify the GitHub webhook HMAC-SHA256 signature.

    Args:
        payload: Raw request body bytes.
        signature: The X-Hub-Signature-256 header value (sha256=...).
        secret: The webhook secret configured in the GitHub App.

    Returns:
        True if the signature is valid.
    """
    if not signature or not secret:
        return False

    if not signature.startswith("sha256="):
        return False

    expected = hmac.new(
        secret.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()

    received = signature[len("sha256="):]
    return hmac.compare_digest(expected, received)
