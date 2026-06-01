"""Tavily web-search backend.

Tavily is an LLM-oriented search API: one call returns ranked results with
snippets and (optionally) extracted page content. SDK is lazy-imported via
plain HTTP so installs that don't use search never pay a dep cost — we use
the shared httpx client rather than the `tavily-python` SDK to keep the
dependency surface small.
"""

from __future__ import annotations

from typing import Any

from devai.adapters.base import AdapterNotConfigured, AdapterNotInstalled
from devai.adapters.web_search.base import WebSearchAdapter, WebSearchResult

_API = "https://api.tavily.com"


class TavilyWebSearchAdapter(WebSearchAdapter):
    provider_name = "tavily"

    def __init__(self, *, api_key: str = "", timeout_seconds: float = 20.0) -> None:
        if not api_key:
            raise AdapterNotConfigured("tavily adapter requires DEVAI_WEB_SEARCH_API_KEY")
        try:
            import httpx  # noqa: F401
        except ImportError as e:
            raise AdapterNotInstalled("tavily adapter requires `pip install httpx`") from e
        self._api_key = api_key
        self._timeout = timeout_seconds
        self._client: Any = None

    def _http(self) -> Any:
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def search(self, query: str, *, max_results: int = 5) -> list[WebSearchResult]:
        resp = await self._http().post(
            f"{_API}/search",
            json={
                "api_key": self._api_key,
                "query": query,
                "max_results": max_results,
                "search_depth": "basic",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        out: list[WebSearchResult] = []
        for hit in data.get("results", [])[:max_results]:
            out.append(
                WebSearchResult(
                    title=hit.get("title", ""),
                    url=hit.get("url", ""),
                    snippet=hit.get("content", ""),
                    score=float(hit.get("score", 0.0) or 0.0),
                )
            )
        return out

    async def fetch(self, url: str) -> WebSearchResult:
        resp = await self._http().post(
            f"{_API}/extract",
            json={"api_key": self._api_key, "urls": [url]},
        )
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results") or []
        if not results:
            return WebSearchResult(url=url, snippet="no content extracted")
        first = results[0]
        return WebSearchResult(url=url, content=first.get("raw_content", "") or first.get("content", ""))

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def health_check(self) -> dict[str, Any]:
        return {"ok": bool(self._api_key), "provider": self.provider_name, "detail": "configured"}


__all__ = ["TavilyWebSearchAdapter"]
