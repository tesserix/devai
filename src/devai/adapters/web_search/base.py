"""WebSearchAdapter ABC + canonical result type.

Gives agents a grounded web-search + fetch capability (the "searches the
web" half of Cursor's feature set) behind a stable interface. Backends:
tavily, brave, exa, … — picked by `DEVAI_WEB_SEARCH_PROVIDER`. The Noop
backend returns an empty result set so a pod with no search key degrades
instead of crashing.
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field
from typing import Any

from devai.adapters.base import Adapter


@dataclass(slots=True)
class WebSearchResult:
    """One search hit. `content` is populated by fetch(), empty for search()."""

    title: str = ""
    url: str = ""
    snippet: str = ""
    content: str = ""
    score: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "content": self.content[:4000] if self.content else "",
            "score": self.score,
        }


class WebSearchAdapter(Adapter):
    """Minimum surface every web-search backend implements."""

    provider_name: str = "abstract"

    @abstractmethod
    async def search(self, query: str, *, max_results: int = 5) -> list[WebSearchResult]:
        """Return ranked results for a query (snippets, no full content)."""

    async def fetch(self, url: str) -> WebSearchResult:
        """Fetch + extract the readable content of a single URL.

        Default: not supported. Backends that can extract page content
        override this; otherwise callers fall back to snippets.
        """
        raise NotImplementedError(f"{self.provider_name} adapter does not implement fetch")

    async def close(self) -> None:
        return None

    async def health_check(self) -> dict[str, Any]:
        return {"ok": True, "provider": self.provider_name, "detail": "no health check implemented"}


__all__ = ["WebSearchAdapter", "WebSearchResult"]
