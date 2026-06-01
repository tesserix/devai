"""Factory — pick the object-store backend from settings.

Reads `settings.object_store_provider` (env: `DEVAI_OBJECT_STORE_PROVIDER`).
Never raises: unknown provider / missing SDK / missing bucket → Noop.
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
from devai.adapters.object_store.base import ObjectStoreAdapter
from devai.adapters.object_store.noop import NoopObjectStoreAdapter

logger = logging.getLogger(__name__)

KNOWN_PROVIDERS = ("noop", "gcs")


def _build_noop(settings: Any) -> ObjectStoreAdapter:
    return NoopObjectStoreAdapter()


def _build_gcs(settings: Any) -> ObjectStoreAdapter:
    from devai.adapters.object_store.gcs import GCSObjectStoreAdapter

    bucket = getattr(settings, "object_store_bucket", "") or ""
    prefix = getattr(settings, "object_store_prefix", "") or ""
    return GCSObjectStoreAdapter(bucket=bucket, prefix=prefix)


object_store_registry: AdapterRegistry[ObjectStoreAdapter] = AdapterRegistry("object_store")
object_store_registry.register("noop", _build_noop)
object_store_registry.register("gcs", _build_gcs)


def create_object_store_adapter(settings: Any) -> ObjectStoreAdapter:
    provider = (getattr(settings, "object_store_provider", "noop") or "noop").lower()
    if not object_store_registry.has(provider):
        logger.warning(
            "object_store_provider=%r unknown (known: %s) — using Noop",
            provider,
            ", ".join(object_store_registry.known()),
        )
        return NoopObjectStoreAdapter()
    try:
        adapter = object_store_registry.resolve(provider, settings)
        logger.info("ObjectStoreAdapter active: %s", adapter.provider_name)
        return adapter
    except (AdapterNotInstalled, AdapterNotConfigured) as e:
        logger.warning("object_store_provider=%s: %s — using Noop", provider, e)
        return NoopObjectStoreAdapter()
    except AdapterError as e:
        logger.warning("object_store_provider=%s failed (%s) — using Noop", provider, e)
        return NoopObjectStoreAdapter()
    except Exception:  # noqa: BLE001
        logger.exception("object_store_provider=%s crashed — using Noop", provider)
        return NoopObjectStoreAdapter()


__all__ = ["KNOWN_PROVIDERS", "create_object_store_adapter", "object_store_registry"]
