"""Web-search adapter family — grounded search + fetch for agents."""

from devai.adapters.web_search.base import WebSearchAdapter, WebSearchResult
from devai.adapters.web_search.factory import create_web_search_adapter, web_search_registry
from devai.adapters.web_search.noop import NoopWebSearchAdapter

__all__ = [
    "NoopWebSearchAdapter",
    "WebSearchAdapter",
    "WebSearchResult",
    "create_web_search_adapter",
    "web_search_registry",
]
