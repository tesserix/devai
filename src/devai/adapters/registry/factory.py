"""Factory — pick the registry backend at runtime.

Reads ``settings.registry_provider`` (env: ``DEVAI_REGISTRY_PROVIDER``) and
returns one of:

    tesserix        → our open-source agentic-registry (/v0/* API)   [default]
    solo_aregistry  → upstream solo.io aregistry (/v0/* API)
    mcp_registry    → official MCP Registry / any /v0.1 compatible registry
    portkey         → Portkey prompt control plane
    noop            → empty catalogs (pipeline falls back to in-tree YAML)

Same graceful-degradation contract as the memory family: any failure
(unknown provider, missing SDK, missing config) falls back to Noop and logs.
The registry is never on the critical path — a registry blip degrades DevAI to
its local specs, it never crashes the pipeline.
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
from devai.adapters.registry.base import RegistryAdapter
from devai.adapters.registry.mcp_registry_adapter import MCPRegistryAdapter
from devai.adapters.registry.noop import NoopRegistryAdapter
from devai.adapters.registry.oidc import resolve_registry_token
from devai.adapters.registry.portkey_adapter import PortkeyRegistryAdapter
from devai.adapters.registry.v0_adapter import V0RegistryAdapter
from devai.registry.client import RegistryClient

logger = logging.getLogger(__name__)

KNOWN_PROVIDERS = ("tesserix", "solo_aregistry", "mcp_registry", "portkey", "noop")


def _v0_client(settings: Any, url_attr: str) -> RegistryClient:
    base_url = getattr(settings, url_attr, "") or getattr(settings, "registry_url", "") or ""
    if not base_url:
        raise AdapterNotConfigured(f"registry adapter requires settings.{url_attr} or registry_url")
    # Prefer a short-lived OIDC client-credentials token (the secure path) over
    # a static token. Falls back to registry_token, then anonymous.
    token = resolve_registry_token(settings)
    timeout = float(getattr(settings, "registry_timeout_seconds", 5.0) or 5.0)
    ttl = float(getattr(settings, "registry_cache_ttl_seconds", 30.0) or 30.0)
    return RegistryClient(base_url=base_url, token=token, timeout_seconds=timeout, ttl_seconds=ttl)


def _build_tesserix(settings: Any) -> RegistryAdapter:
    return V0RegistryAdapter(_v0_client(settings, "registry_url"), provider_name="tesserix")


def _build_solo(settings: Any) -> RegistryAdapter:
    return V0RegistryAdapter(_v0_client(settings, "solo_registry_url"), provider_name="solo_aregistry")


def _build_mcp(settings: Any) -> RegistryAdapter:
    base_url = getattr(settings, "mcp_registry_url", "") or getattr(settings, "registry_url", "") or ""
    if not base_url:
        raise AdapterNotConfigured("mcp_registry adapter requires settings.mcp_registry_url or registry_url")
    token = getattr(settings, "registry_token", "") or ""
    return MCPRegistryAdapter(base_url=base_url, token=token)


def _build_portkey(settings: Any) -> RegistryAdapter:
    api_key = getattr(settings, "portkey_api_key", "") or ""
    if not api_key:
        raise AdapterNotConfigured("portkey adapter requires settings.portkey_api_key")
    return PortkeyRegistryAdapter(api_key=api_key)


def _build_noop(_settings: Any) -> RegistryAdapter:
    return NoopRegistryAdapter()


registry_registry: AdapterRegistry[RegistryAdapter] = AdapterRegistry("registry")
registry_registry.register("tesserix", _build_tesserix)
registry_registry.register("solo_aregistry", _build_solo)
registry_registry.register("mcp_registry", _build_mcp)
registry_registry.register("portkey", _build_portkey)
registry_registry.register("noop", _build_noop)


def create_registry_adapter(settings: Any) -> RegistryAdapter:
    """Resolve ``settings.registry_provider`` to a constructed adapter.

    Never raises — on any error returns a NoopRegistryAdapter and logs why.
    """
    provider = (getattr(settings, "registry_provider", "tesserix") or "tesserix").lower()
    if not registry_registry.has(provider):
        logger.warning(
            "registry_provider=%r unknown (known: %s) — using Noop",
            provider,
            ", ".join(registry_registry.known()),
        )
        return NoopRegistryAdapter()
    try:
        adapter = registry_registry.resolve(provider, settings)
        logger.info("RegistryAdapter active: %s", adapter.provider_name)
        return adapter
    except (AdapterNotConfigured, AdapterNotInstalled, AdapterError) as e:
        logger.warning("registry_provider=%s: %s — using Noop", provider, e)
        return NoopRegistryAdapter()
    except Exception:  # noqa: BLE001
        logger.exception("registry_provider=%s crashed during build — using Noop", provider)
        return NoopRegistryAdapter()


__all__ = ["KNOWN_PROVIDERS", "create_registry_adapter", "registry_registry"]
