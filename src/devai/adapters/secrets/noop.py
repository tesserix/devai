"""Noop secrets adapter — mandatory graceful-degrade fallback.

Used in tests, when ``DEVAI_SECRETS_PROVIDER`` is unset/unknown, or when the
GCP SM SDK / write IAM is missing. It cannot store secret values, so
``can_write`` is False and ``set_secret`` raises — callers learn that secret
provisioning is unavailable rather than silently losing a value.
"""

from __future__ import annotations

from typing import Any

from devai.adapters.base import AdapterError
from devai.adapters.secrets.base import SecretRef, SecretsAdapter


class NoopSecretsAdapter(SecretsAdapter):
    """A secrets adapter that stores nothing."""

    provider_name = "noop"

    async def can_write(self) -> bool:
        return False

    async def set_secret(
        self,
        key: str,
        value: str,
        *,
        labels: dict[str, str] | None = None,
    ) -> SecretRef:
        raise AdapterError(
            "secrets backend is disabled (noop) — cannot store secret values. "
            "Set DEVAI_SECRETS_PROVIDER=gcp_sm and grant write IAM."
        )

    async def get_secret(self, ref: SecretRef | str) -> str | None:
        return None

    async def delete_secret(self, ref: SecretRef | str) -> bool:
        return True

    async def health_check(self) -> dict[str, Any]:
        return {"ok": True, "provider": "noop", "detail": "secrets disabled"}


__all__ = ["NoopSecretsAdapter"]
