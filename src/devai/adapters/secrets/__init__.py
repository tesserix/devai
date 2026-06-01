"""Secrets adapter family — runtime secret resolution + provisioning.

Backends: gcp_sm (Google Secret Manager, read+write), env (read-only),
noop (disabled). Secret *values* live only in the backend; the Settings store
persists only ``SecretRef`` pointers.

    from devai.adapters.secrets import (
        SecretsAdapter, SecretRef, create_secrets_adapter,
    )
"""

from __future__ import annotations

from devai.adapters.secrets.base import SecretRef, SecretsAdapter
from devai.adapters.secrets.factory import create_secrets_adapter, secrets_registry
from devai.adapters.secrets.noop import NoopSecretsAdapter

__all__ = [
    "NoopSecretsAdapter",
    "SecretRef",
    "SecretsAdapter",
    "create_secrets_adapter",
    "secrets_registry",
]
