"""Factory — pick the web-search backend from settings.

Reads `settings.web_search_provider` (env: `DEVAI_WEB_SEARCH_PROVIDER`).
Never raises: unknown provider / missing SDK / missing key → Noop.
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
from devai.adapters.web_search.base import WebSearchAdapter
from devai.adapters.web_search.noop import NoopWebSearchAdapter

logger = logging.getLogger(__name__)

KNOWN_PROVIDERS = ("noop", "tavily")


def _build_noop(settings: Any) -> WebSearchAdapter:
    return NoopWebSearchAdapter()


def _build_tavily(settings: Any) -> WebSearchAdapter:
    from devai.adapters.web_search.tavily import TavilyWebSearchAdapter

    api_key = getattr(settings, "web_search_api_key", "") or ""
    return TavilyWebSearchAdapter(api_key=api_key)


web_search_registry: AdapterRegistry[WebSearchAdapter] = AdapterRegistry("web_search")
web_search_registry.register("noop", _build_noop)
web_search_registry.register("tavily", _build_tavily)


def create_web_search_adapter(settings: Any) -> WebSearchAdapter:
    provider = (getattr(settings, "web_search_provider", "noop") or "noop").lower()
    if not web_search_registry.has(provider):
        logger.warning(
            "web_search_provider=%r unknown (known: %s) — using Noop",
            provider,
            ", ".join(web_search_registry.known()),
        )
        return NoopWebSearchAdapter()
    try:
        adapter = web_search_registry.resolve(provider, settings)
        logger.info("WebSearchAdapter active: %s", adapter.provider_name)
        return adapter
    except (AdapterNotInstalled, AdapterNotConfigured) as e:
        logger.warning("web_search_provider=%s: %s — using Noop", provider, e)
        return NoopWebSearchAdapter()
    except AdapterError as e:
        logger.warning("web_search_provider=%s failed (%s) — using Noop", provider, e)
        return NoopWebSearchAdapter()
    except Exception:  # noqa: BLE001
        logger.exception("web_search_provider=%s crashed — using Noop", provider)
        return NoopWebSearchAdapter()


__all__ = ["KNOWN_PROVIDERS", "create_web_search_adapter", "web_search_registry"]
