"""Reverse proxy in front of a sandbox's code-server.

The IDE has no ingress of its own — it is reachable only from the control plane
(the sandbox NetworkPolicy) and only through the API, which applies the same
ownership check as every other workspace route. That is what lets the editor
itself run unauthenticated: containment, not a second password.
"""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING, Any

import httpx
from fastapi import Response

from devai.sandbox.workspace import IDE_PORT

if TYPE_CHECKING:
    from fastapi import Request, WebSocket

logger = logging.getLogger(__name__)

_TIMEOUT = 60.0

# Hop-by-hop headers belong to the connection, not the message.
_DROP = {"content-length", "content-encoding", "transfer-encoding", "connection", "keep-alive"}


def ide_upstream_url(endpoint: str, path: str) -> str:
    """The IDE sits on its own port of the same workspace Service."""
    host = endpoint.split("://")[-1].split("/")[0].rsplit(":", 1)[0]
    return f"http://{host}:{IDE_PORT}/{path.lstrip('/')}"


async def proxy_ide_request(request: Request, endpoint: str, path: str) -> Response:
    """Pass one HTTP request through to the editor, verbatim."""
    url = ide_upstream_url(endpoint, path)
    headers = {k: v for k, v in request.headers.items() if k.lower() not in {*_DROP, "host"}}
    # The editor is served under /api/sandboxes/<id>/ide/, so it has to be told
    # where it is or every asset URL it builds points at the API root.
    prefix = request.url.path.removesuffix(path)
    headers["X-Forwarded-Prefix"] = prefix
    headers["X-Forwarded-Host"] = request.url.netloc
    headers["X-Forwarded-Proto"] = request.url.scheme
    body = await request.body()
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as http:
            upstream = await http.request(
                request.method, url, headers=headers, content=body, params=request.query_params
            )
    except httpx.HTTPError as e:
        # An editor that is still starting is a state, not a server error.
        return Response(content=f"editor unavailable: {e}", status_code=502, media_type="text/plain")
    passthrough: dict[str, Any] = {k: v for k, v in upstream.headers.items() if k.lower() not in _DROP}
    return Response(content=upstream.content, status_code=upstream.status_code, headers=passthrough)


async def proxy_ide_socket(websocket: WebSocket, endpoint: str, path: str) -> None:
    """Bridge the browser's socket to the editor's, frame for frame."""
    import asyncio

    from websockets.asyncio.client import connect

    url = "ws" + ide_upstream_url(endpoint, path)[4:]
    await websocket.accept()
    try:
        async with connect(url, open_timeout=_TIMEOUT, max_size=None) as upstream:

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

            done, pending = await asyncio.wait(
                [asyncio.create_task(to_upstream()), asyncio.create_task(to_browser())],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
    except Exception:  # noqa: BLE001 — a dropped editor closes the socket, it does not 500 the API
        logger.debug("ide socket to %s ended", url, exc_info=True)
    finally:
        with contextlib.suppress(RuntimeError):
            await websocket.close()


__all__ = ["ide_upstream_url", "proxy_ide_request", "proxy_ide_socket"]
