"""Web search + fetch tools — the "searches the web" Cursor capability.

Registered into `devai.tools.registry` so any specialization can opt in via
`allowed_tools: [web_search, web_fetch]`. Execution routes through the
`WebSearchAdapter` on the `ToolContext` (Tavily in prod, Noop when
disabled), so a spec that wants search degrades to "no results" instead of
erroring when the platform has search turned off.
"""

from __future__ import annotations

import json
from typing import Any

from devai.tools.registry import Handler, ToolContext, register

_SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "What to search the web for"},
        "max_results": {"type": "integer", "description": "Max results (default 5)"},
    },
    "required": ["query"],
}

_FETCH_SCHEMA = {
    "type": "object",
    "properties": {
        "url": {"type": "string", "description": "URL to fetch and extract readable content from"},
    },
    "required": ["url"],
}


def _web_search_factory(ctx: ToolContext) -> Handler:
    async def handler(args: dict[str, Any]) -> str:
        if ctx.web_search is None:
            return "ERROR: web search is not available in this run"
        query = (args.get("query") or "").strip()
        if not query:
            return "ERROR: query is required"
        max_results = int(args.get("max_results") or 5)
        try:
            results = await ctx.web_search.search(query, max_results=max_results)
        except Exception as e:  # noqa: BLE001
            return f"ERROR: web search failed: {e}"
        if not results:
            return "No results."
        return json.dumps([r.to_dict() for r in results], indent=2)

    return handler


def _web_fetch_factory(ctx: ToolContext) -> Handler:
    async def handler(args: dict[str, Any]) -> str:
        if ctx.web_search is None:
            return "ERROR: web fetch is not available in this run"
        url = (args.get("url") or "").strip()
        if not url:
            return "ERROR: url is required"
        try:
            result = await ctx.web_search.fetch(url)
        except NotImplementedError:
            return "ERROR: the active web-search provider cannot fetch page content"
        except Exception as e:  # noqa: BLE001
            return f"ERROR: web fetch failed: {e}"
        return result.content[:8000] or result.snippet or "(no content)"

    return handler


def register_web_tools(*, overwrite: bool = False) -> None:
    register(
        "web_search",
        "Search the web for up-to-date information. Returns ranked results with snippets.",
        _SEARCH_SCHEMA,
        _web_search_factory,
        overwrite=overwrite,
    )
    register(
        "web_fetch",
        "Fetch a URL and return its readable page content.",
        _FETCH_SCHEMA,
        _web_fetch_factory,
        overwrite=overwrite,
    )


register_web_tools()

__all__ = ["register_web_tools"]
