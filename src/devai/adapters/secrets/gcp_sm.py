"""GCP Secret Manager secrets adapter.

Auto-provisions per-user / per-tenant secrets into Google Secret Manager using
the ``google-cloud-secret-manager`` SDK (not the ``gcloud`` CLI). It:

  - ``set_secret`` — creates the secret if absent (``create_secret``) then adds a
    new version (``add_secret_version``). Requires write IAM
    (``roles/secretmanager.admin`` or ``secretVersionManager`` + ``secretCreator``).
  - ``get_secret`` — accesses the latest (or pinned) version.
  - ``delete_secret`` — deletes the whole secret.
  - ``can_write`` — best-effort probe (lists secrets) to tell the UI whether
    provisioning will work, so the Settings page can show a read-only banner
    instead of failing on save.

Per the project guardrail, secret *values* live only here in GCP SM; the
Settings store persists only the returned ``SecretRef`` (the secret id).

The SDK is imported lazily inside ``__init__`` so a deployment that doesn't use
this provider never pulls it in. Auth uses Application Default Credentials
(Workload Identity in-cluster), so no key material is handled by this code.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from devai.adapters.base import AdapterError, AdapterNotConfigured, AdapterNotInstalled
from devai.adapters.secrets.base import SecretRef, SecretsAdapter

logger = logging.getLogger(__name__)

# GCP secret ids: letters, digits, hyphen, underscore; <= 255 chars.
_VALID_ID = re.compile(r"[^A-Za-z0-9_-]")


def sanitize_secret_id(key: str) -> str:
    """Map a logical key to a GCP-SM-legal secret id (deterministic)."""
    cleaned = _VALID_ID.sub("-", key).strip("-")
    return cleaned[:255] or "devai-secret"


class GcpSecretManagerAdapter(SecretsAdapter):
    """Secrets stored in Google Secret Manager."""

    provider_name = "gcp_sm"

    def __init__(self, settings: Any) -> None:
        self._project = (
            getattr(settings, "secrets_gcp_project", "")
            or getattr(settings, "gke_project", "")
            or getattr(settings, "gcp_project", "")
        )
        if not self._project:
            raise AdapterNotConfigured(
                "gcp_sm secrets adapter requires a GCP project (DEVAI_SECRETS_GCP_PROJECT or DEVAI_GKE_PROJECT)"
            )
        try:
            from google.cloud import secretmanager_v1  # lazy
        except ImportError as e:
            raise AdapterNotInstalled("google-cloud-secret-manager is not installed") from e
        self._sm = secretmanager_v1
        # Async client uses ADC (Workload Identity in-cluster).
        self._client = secretmanager_v1.SecretManagerServiceAsyncClient()
        self._parent = f"projects/{self._project}"
        self._can_write: bool | None = None

    # --- helpers ----------------------------------------------------------

    def _secret_path(self, secret_id: str) -> str:
        return f"{self._parent}/secrets/{secret_id}"

    def _version_path(self, secret_id: str, version: str = "latest") -> str:
        return f"{self._secret_path(secret_id)}/versions/{version}"

    @staticmethod
    def _id_of(ref: SecretRef | str) -> str:
        return ref.name if isinstance(ref, SecretRef) else str(ref)

    def ref_for(self, key: str) -> SecretRef:
        return SecretRef(name=sanitize_secret_id(key), provider=self.provider_name)

    # --- contract ---------------------------------------------------------

    async def can_write(self) -> bool:
        """Best-effort: can we create secrets? Cached after first probe."""
        if self._can_write is not None:
            return self._can_write
        try:
            # Listing requires read; a definitive write probe would create a
            # throwaway secret. We do the cheap read probe and assume write IAM
            # is co-granted (the deploy grants admin or none). set_secret still
            # surfaces a real PermissionDenied if not.
            it = await self._client.list_secrets(request={"parent": self._parent, "page_size": 1})
            # Touch the iterator so the RPC actually fires.
            async for _ in it:  # noqa: B007
                break
            self._can_write = True
        except Exception as e:  # noqa: BLE001
            logger.warning("gcp_sm can_write probe failed: %s", e)
            self._can_write = False
        return self._can_write

    async def set_secret(
        self,
        key: str,
        value: str,
        *,
        labels: dict[str, str] | None = None,
    ) -> SecretRef:
        secret_id = sanitize_secret_id(key)
        # GCP labels: lowercase letters/digits/_/-, <=63 chars; sanitize.
        gcp_labels = {_label_key(k): _label_val(v) for k, v in (labels or {}).items()}
        try:
            # Create the secret container if it doesn't exist yet.
            try:
                await self._client.create_secret(
                    request={
                        "parent": self._parent,
                        "secret_id": secret_id,
                        "secret": {
                            "replication": {"automatic": {}},
                            "labels": {"managed-by": "devai", **gcp_labels},
                        },
                    }
                )
                logger.info("gcp_sm: created secret %s", secret_id)
            except Exception as e:  # AlreadyExists is fine
                if "AlreadyExists" not in str(e) and " 409 " not in str(e):
                    raise
            # Add the new value as a version.
            await self._client.add_secret_version(
                request={
                    "parent": self._secret_path(secret_id),
                    "payload": {"data": value.encode("utf-8")},
                }
            )
            self._can_write = True
            return SecretRef(name=secret_id, provider=self.provider_name, labels=dict(labels or {}))
        except Exception as e:  # noqa: BLE001
            self._can_write = False
            raise AdapterError(f"gcp_sm set_secret({secret_id}) failed: {e}") from e

    async def get_secret(self, ref: SecretRef | str) -> str | None:
        secret_id = self._id_of(ref)
        version = ref.version if isinstance(ref, SecretRef) else "latest"
        try:
            resp = await self._client.access_secret_version(request={"name": self._version_path(secret_id, version)})
            return resp.payload.data.decode("utf-8")
        except Exception as e:  # noqa: BLE001
            logger.warning("gcp_sm get_secret(%s) failed: %s", secret_id, e)
            return None

    async def delete_secret(self, ref: SecretRef | str) -> bool:
        secret_id = self._id_of(ref)
        try:
            await self._client.delete_secret(request={"name": self._secret_path(secret_id)})
            return True
        except Exception as e:  # noqa: BLE001
            if "NotFound" in str(e) or " 404 " in str(e):
                return True
            logger.warning("gcp_sm delete_secret(%s) failed: %s", secret_id, e)
            return False

    async def close(self) -> None:
        # The async transport closes with the event loop; nothing to free.
        return None

    async def health_check(self) -> dict[str, Any]:
        ok = await self.can_write()
        return {
            "ok": True,
            "provider": self.provider_name,
            "project": self._project,
            "writable": ok,
            "detail": "gcp secret manager" if ok else "read-only or unreachable",
        }


def _label_key(k: str) -> str:
    return re.sub(r"[^a-z0-9_-]", "-", k.lower())[:63] or "k"


def _label_val(v: str) -> str:
    return re.sub(r"[^a-z0-9_-]", "-", str(v).lower())[:63]


__all__ = ["GcpSecretManagerAdapter", "sanitize_secret_id"]
