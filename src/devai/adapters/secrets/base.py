"""SecretsAdapter ABC + canonical secret reference record.

The secrets family is the runtime secret-resolution layer for the Settings
capability. The Settings store never holds secret *values* — only *references*
(``SecretRef``). The adapter is what actually writes a value into the backing
secret store (GCP Secret Manager, AWS SM, Vault) and reads it back.

Hard rules (per CLAUDE.md §6 + the project's "secrets in GCP SM only" guardrail):

  - **Values never touch the app DB.** ``set_secret`` writes to the backend;
    the DB only ever stores the returned ``SecretRef`` (a name/path, no value).
  - **The factory degrades, the adapter does not lie.** ``create_secrets_adapter``
    falls back to Noop on a missing SDK / missing config. But a configured
    backend that lacks *write* permission must surface that as a failed
    ``set_secret`` (ok=False / raise), not silently pretend it stored the value.
  - **Provider-native naming is the adapter's job.** Callers pass a logical
    ``key``; the adapter maps it to the backend's namespace via ``ref_for``.
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field
from typing import Any

from devai.adapters.base import Adapter


@dataclass(slots=True)
class SecretRef:
    """A pointer to a secret value living in the backend — never the value.

    ``name`` is the backend-native identifier (e.g. a GCP SM secret id). This is
    what gets persisted in the Settings store and what the adapter resolves back
    to a value at read time.
    """

    name: str
    provider: str = ""
    version: str = "latest"
    # Optional non-sensitive metadata (scope, connector, who created it).
    labels: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "provider": self.provider,
            "version": self.version,
            "labels": dict(self.labels),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> SecretRef | None:
        if not data or not data.get("name"):
            return None
        return cls(
            name=data["name"],
            provider=data.get("provider", ""),
            version=data.get("version", "latest"),
            labels=dict(data.get("labels") or {}),
        )


class SecretsAdapter(Adapter):
    """Minimum surface every secret backend implements.

      - ``set_secret``    — create-or-update a secret value, return its SecretRef
      - ``get_secret``    — resolve a SecretRef (or name) back to its value
      - ``delete_secret`` — remove a secret
      - ``can_write``     — whether this backend instance can create/update
        secrets (write IAM present). Drives the Settings UI's "read-only" state.

    Backends lazily import their SDK. ``set_secret`` must raise / return falsey
    rather than silently no-op when the backend lacks write permission.
    """

    #: Set by subclass — used in logs + health output.
    provider_name: str = "abstract"

    @abstractmethod
    async def can_write(self) -> bool:
        """Whether this instance can create/update secrets (write IAM present)."""

    @abstractmethod
    async def set_secret(
        self,
        key: str,
        value: str,
        *,
        labels: dict[str, str] | None = None,
    ) -> SecretRef:
        """Create or update a secret value; return its backend reference.

        ``key`` is a logical, filesystem-safe id the caller chose (e.g.
        ``devai-user-<uid>-llm-anthropic_api_key``). The adapter maps it to the
        backend namespace. Raises ``AdapterError`` if the backend can't write.
        """

    @abstractmethod
    async def get_secret(self, ref: SecretRef | str) -> str | None:
        """Resolve a SecretRef (or raw name) to its current value, or None."""

    @abstractmethod
    async def delete_secret(self, ref: SecretRef | str) -> bool:
        """Delete the secret. Returns True if removed (or already absent)."""

    def ref_for(self, key: str) -> SecretRef:
        """Build the SecretRef this adapter would use for ``key`` (no I/O)."""
        return SecretRef(name=key, provider=self.provider_name)

    # --- Adapter contract defaults ---------------------------------------

    async def close(self) -> None:
        return None

    async def health_check(self) -> dict[str, Any]:
        return {"ok": True, "provider": self.provider_name, "detail": "secrets adapter"}


__all__ = ["SecretRef", "SecretsAdapter"]
