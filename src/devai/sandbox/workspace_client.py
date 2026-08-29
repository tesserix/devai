"""API-side client for a sandbox workspace.

The workspace has no ingress route: it is reached over the cluster-internal
Service with the per-sandbox capability token, which the API reads from the
workspace Secret rather than storing anywhere queryable.
"""

from __future__ import annotations

import builtins
from typing import Any

import httpx

from devai.sandbox.browser_proxy import TOKEN_HEADER

_TIMEOUT = 130.0


class WorkspaceClient:
    def __init__(self, endpoint: str, *, token: str, timeout: float = _TIMEOUT) -> None:
        self._base = endpoint if endpoint.startswith("http") else f"http://{endpoint}"
        self._token = token
        self._timeout = timeout

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self._timeout) as http:
            resp = await http.post(f"{self._base}{path}", json=body, headers={TOKEN_HEADER: self._token})
        resp.raise_for_status()
        payload: dict[str, Any] = resp.json()
        return payload

    async def read(self, path: str) -> str:
        return str((await self._post("/file/read", {"path": path}))["content"])

    async def write(self, path: str, content: str) -> dict[str, Any]:
        return await self._post("/file/write", {"path": path, "content": content})

    async def list(self, path: str = ".") -> builtins.list[str]:
        entries: builtins.list[str] = (await self._post("/file/list", {"path": path}))["entries"]
        return entries

    async def search(self, needle: str, path: str = ".") -> builtins.list[dict[str, Any]]:
        hits: builtins.list[dict[str, Any]] = (await self._post("/file/search", {"needle": needle, "path": path}))[
            "hits"
        ]
        return hits

    async def preview_ports(self) -> builtins.list[int]:
        async with httpx.AsyncClient(timeout=self._timeout) as http:
            resp = await http.get(f"{self._base}/preview/ports", headers={TOKEN_HEADER: self._token})
        resp.raise_for_status()
        ports: builtins.list[int] = resp.json()["ports"]
        return ports

    async def preview_fetch(self, port: int, path: str) -> dict[str, Any]:
        return await self._post("/preview/fetch", {"port": port, "path": path})

    async def exec(self, command: str, *, timeout: float = 120.0) -> dict[str, Any]:
        return await self._post("/shell/exec", {"command": command, "timeout": timeout})

    async def browser_navigate(self, url: str) -> dict[str, Any]:
        return await self._post("/browser/navigate", {"url": url})

    async def browser_screenshot(self, *, full_page: bool = False) -> dict[str, Any]:
        return await self._post("/browser/screenshot", {"full_page": full_page})

    async def browser_click(self, selector: str) -> dict[str, Any]:
        return await self._post("/browser/click", {"selector": selector})

    async def browser_type(self, selector: str, text: str) -> dict[str, Any]:
        return await self._post("/browser/type", {"selector": selector, "text": text})

    async def browser_scroll(self, *, delta_x: float = 0, delta_y: float = 600) -> dict[str, Any]:
        return await self._post("/browser/scroll", {"delta_x": delta_x, "delta_y": delta_y})

    async def browser_get_content(self, *, selector: str = "") -> dict[str, Any]:
        return await self._post("/browser/content", {"selector": selector})


__all__ = ["WorkspaceClient"]
