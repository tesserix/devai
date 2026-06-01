"""Noop web-search backend — used when search is disabled or unconfigured."""

from __future__ import annotations

from typing import Any

from devai.adapters.web_search.base import WebSearchAdapter, WebSearchResult


class NoopWebSearchAdapter(WebSearchAdapter):
    provider_name = "noop"

    async def search(self, query: str, *, max_results: int = 5) -> list[WebSearchResult]:
        return []

    async def fetch(self, url: str) -> WebSearchResult:
        return WebSearchResult(url=url, snippet="web search disabled (noop provider)")

    async def health_check(self) -> dict[str, Any]:
        return {"ok": True, "provider": self.provider_name, "detail": "disabled"}


__all__ = ["NoopWebSearchAdapter"]
