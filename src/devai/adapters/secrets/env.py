"""Env-var secrets adapter — read-only resolution from the process environment.

For local/dev where secrets are injected as ``DEVAI_*`` env vars (or via the
ExternalSecret → env path in prod). It can *read* secrets back by name but
cannot *write* — ``set_secret`` raises, so the Settings UI shows read-only and
provisioning is routed to gcp_sm instead. Useful as the resolve-side backend
when GCP SM write IAM isn't granted but values are already in the env.
"""

from __future__ import annotations

import os
from typing import Any

from devai.adapters.base import AdapterError
from devai.adapters.secrets.base import SecretRef, SecretsAdapter


class EnvSecretsAdapter(SecretsAdapter):
    """Resolve secrets from environment variables (read-only)."""

    provider_name = "env"

    def __init__(self, settings: Any = None) -> None:  # noqa: ARG002
        self._settings = settings

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
            "env secrets adapter is read-only — cannot provision secrets. "
            "Use DEVAI_SECRETS_PROVIDER=gcp_sm to auto-create secrets."
        )

    async def get_secret(self, ref: SecretRef | str) -> str | None:
        name = ref.name if isinstance(ref, SecretRef) else str(ref)
        # Try the name verbatim, then an upper-cased env-style variant.
        return os.environ.get(name) or os.environ.get(name.upper().replace("-", "_"))

    async def delete_secret(self, ref: SecretRef | str) -> bool:
        return False

    async def health_check(self) -> dict[str, Any]:
        return {"ok": True, "provider": "env", "writable": False, "detail": "env vars (read-only)"}


__all__ = ["EnvSecretsAdapter"]
