"""Factory for the secrets adapter family.

``create_secrets_adapter(settings)`` reads ``settings.secrets_provider`` and
returns a constructed adapter. Never raises — unknown provider / missing SDK /
missing config degrades to ``NoopSecretsAdapter`` (which refuses writes loudly),
so a misconfigured pod never crashes on boot.
"""

from __future__ import annotations

import logging
from typing import Any

from devai.adapters.base import (
    AdapterError,
    AdapterNotConfigured,
    AdapterNotInstalled,
    AdapterRegistry,
)
from devai.adapters.secrets.base import SecretsAdapter
from devai.adapters.secrets.noop import NoopSecretsAdapter

logger = logging.getLogger(__name__)


def _build_noop(_settings: Any) -> SecretsAdapter:
    return NoopSecretsAdapter()


def _build_env(settings: Any) -> SecretsAdapter:
    from devai.adapters.secrets.env import EnvSecretsAdapter

    return EnvSecretsAdapter(settings)


def _build_gcp_sm(settings: Any) -> SecretsAdapter:
    from devai.adapters.secrets.gcp_sm import GcpSecretManagerAdapter

    return GcpSecretManagerAdapter(settings)


secrets_registry: AdapterRegistry[SecretsAdapter] = AdapterRegistry("secrets")
secrets_registry.register("noop", _build_noop)
secrets_registry.register("env", _build_env)
secrets_registry.register("gcp_sm", _build_gcp_sm)


def create_secrets_adapter(settings: Any) -> SecretsAdapter:
    """Resolve ``settings.secrets_provider`` to a constructed adapter.

    Never raises — degrades to Noop on any error.
    """
    provider = (getattr(settings, "secrets_provider", "noop") or "noop").lower()
    if not secrets_registry.has(provider):
        logger.warning(
            "secrets_provider=%r is unknown (known: %s) — using Noop",
            provider,
            ", ".join(secrets_registry.known()),
        )
        return NoopSecretsAdapter()

    try:
        adapter = secrets_registry.resolve(provider, settings)
        logger.info("SecretsAdapter active: %s", adapter.provider_name)
        return adapter
    except AdapterNotInstalled as e:
        logger.warning("secrets_provider=%s: %s — using Noop", provider, e)
        return NoopSecretsAdapter()
    except AdapterNotConfigured as e:
        logger.warning("secrets_provider=%s: %s — using Noop", provider, e)
        return NoopSecretsAdapter()
    except AdapterError as e:
        logger.warning("secrets_provider=%s failed to build (%s) — using Noop", provider, e)
        return NoopSecretsAdapter()
    except Exception:  # noqa: BLE001
        logger.exception("secrets_provider=%s crashed during build — using Noop", provider)
        return NoopSecretsAdapter()


__all__ = ["create_secrets_adapter", "secrets_registry"]
