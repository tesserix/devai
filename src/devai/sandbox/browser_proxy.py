"""Two-hop reverse proxy from an owner session to loopback-only noVNC."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import httpx
from fastapi import Response

from devai.sandbox.workspace import BROWSER_PORT

if TYPE_CHECKING:
    from fastapi import Request, WebSocket

logger = logging.getLogger(__name__)

TOKEN_HEADER = "X-DevAI-Workspace-Token"
_TIMEOUT = 60.0
_DROP = {"content-length", "content-encoding", "transfer-encoding", "connection", "keep-alive"}
_SAFE_REQUEST_HEADERS = {
    "accept",
    "accept-encoding",
    "accept-language",
    "cache-control",
    "content-type",
    "if-modified-since",
    "if-none-match",
    "range",
    "user-agent",
}


def browser_proxy_headers(headers: Mapping[str, str], *, token: str = "") -> dict[str, str]:
    """Forward browser mechanics, never the user's session or identity."""
    forwarded = {key: value for key, value in headers.items() if key.lower() in _SAFE_REQUEST_HEADERS}
    if token:
        forwarded[TOKEN_HEADER] = token
    return forwarded


def _workspace_url(endpoint: str, path: str) -> str:
    base = endpoint if endpoint.startswith("http") else f"http://{endpoint}"
    return f"{base.rstrip('/')}/browser/desktop/{path.lstrip('/')}"


def _desktop_url(path: str) -> str:
    return f"http://127.0.0.1:{BROWSER_PORT}/{path.lstrip('/')}"


async def _proxy_request(
    request: Request,
    url: str,
    *,
    token: str = "",
) -> Response:
    forwarded = browser_proxy_headers(request.headers, token=token)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as http:
            upstream = await http.request(
                request.method,
                url,
                headers=forwarded,
                content=await request.body(),
                params=request.query_params,
            )
    except httpx.HTTPError:
        return Response(content="browser desktop unavailable", status_code=502, media_type="text/plain")
    passthrough: dict[str, Any] = {key: value for key, value in upstream.headers.items() if key.lower() not in _DROP}
    return Response(content=upstream.content, status_code=upstream.status_code, headers=passthrough)


async def proxy_browser_request(request: Request, endpoint: str, path: str, token: str) -> Response:
    return await _proxy_request(request, _workspace_url(endpoint, path), token=token)


async def proxy_desktop_request(request: Request, path: str) -> Response:
    return await _proxy_request(request, _desktop_url(path))


async def _bridge_socket(websocket: WebSocket, url: str, *, additional_headers: dict[str, str] | None = None) -> None:
    import asyncio

    from websockets.asyncio.client import connect

    await websocket.accept()
    try:
        async with connect(
            url,
            additional_headers=additional_headers,
            open_timeout=_TIMEOUT,
            max_size=None,
        ) as upstream:

            async def to_upstream() -> None:
                while True:
                    message = await websocket.receive()
                    if message.get("type") == "websocket.disconnect":
                        return
                    data = message.get("bytes")
                    await upstream.send(data if data is not None else message.get("text", ""))

            async def to_browser() -> None:
                async for frame in upstream:
                    if isinstance(frame, bytes):
                        await websocket.send_bytes(frame)
                    else:
                        await websocket.send_text(frame)

            tasks = [asyncio.create_task(to_upstream()), asyncio.create_task(to_browser())]
            _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
    except Exception:  # noqa: BLE001 — a desktop disconnect closes only this socket
        logger.debug("browser desktop socket to %s ended", url, exc_info=True)
    finally:
        with contextlib.suppress(RuntimeError):
            await websocket.close()


async def proxy_browser_socket(websocket: WebSocket, endpoint: str, path: str, token: str) -> None:
    url = "ws" + _workspace_url(endpoint, path)[4:]
    await _bridge_socket(websocket, url, additional_headers={TOKEN_HEADER: token})


async def proxy_desktop_socket(websocket: WebSocket, path: str) -> None:
    url = "ws" + _desktop_url(path)[4:]
    await _bridge_socket(websocket, url)


__all__ = [
    "TOKEN_HEADER",
    "browser_proxy_headers",
    "proxy_browser_request",
    "proxy_browser_socket",
    "proxy_desktop_request",
    "proxy_desktop_socket",
]
