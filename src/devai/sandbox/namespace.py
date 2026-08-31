"""The sandbox's own namespace.

The boundary is the namespace object itself, so isolation no longer depends on
every label selector being right. Deleting the namespace deletes everything the
sandbox ever created.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from devai.sandbox.job import SANDBOX_LABEL, SANDBOX_SERVICE_ACCOUNT

if TYPE_CHECKING:
    from devai.sandbox.models import SandboxRecord

_PREFIX = "devai-sbx-"


def sandbox_namespace(sandbox_id: str) -> str:
    """`devai-sbx-<uuid>` — 46 chars, inside the 63-char RFC1123 limit."""
    return f"{_PREFIX}{sandbox_id}"


def recorded_namespace(record: SandboxRecord) -> str:
    """The namespace this sandbox was provisioned into; '' for legacy records."""
    return str((record.detail or {}).get("namespace") or "")


def build_namespace_manifest(record: SandboxRecord) -> dict[str, Any]:
    # Owner may be an email; a short hash keeps it label-safe and out of
    # cluster metadata while staying attributable through the sandbox row.
    owner_hash = hashlib.sha256(record.owner.encode()).hexdigest()[:16]
    return {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": sandbox_namespace(record.id),
            "labels": {
                "app.kubernetes.io/managed-by": "devai",
                SANDBOX_LABEL: record.id,
                "devai.tesserix.app/owner-hash": owner_hash,
                # The kubelet refuses privileged/root pods here even if a
                # manifest builder regresses.
                "pod-security.kubernetes.io/enforce": "restricted",
            },
        },
    }


def build_service_account_manifest(namespace: str) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": {
            "name": SANDBOX_SERVICE_ACCOUNT,
            "namespace": namespace,
            "labels": {"app.kubernetes.io/managed-by": "devai"},
        },
        "automountServiceAccountToken": False,
    }


__all__ = [
    "build_namespace_manifest",
    "build_service_account_manifest",
    "recorded_namespace",
    "sandbox_namespace",
]
