"""Shared ASGI guardrails for stateless MCP 2026-07-28 endpoints."""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.responses import JSONResponse, Response

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send


def stateless_asgi(app: ASGIApp, *, normalize_mount: bool = False) -> ASGIApp:
    """Reject session/event-stream semantics before dispatching to an MCP app."""

    async def guarded(scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") == "http":
            headers = {key.lower(): value for key, value in scope.get("headers", [])}
            if b"mcp-session-id" in headers:
                await JSONResponse(
                    {"jsonrpc": "2.0", "error": {"code": -32600, "message": "invalid session"}}, status_code=404
                )(scope, receive, send)
                return
            if scope.get("method") == "GET":
                await Response(status_code=405)(scope, receive, send)
                return
            if normalize_mount and scope.get("path", "") in ("", scope.get("root_path", "")):
                scope = dict(scope)
                scope["path"] = "/"
        await app(scope, receive, send)

    return guarded


__all__ = ["stateless_asgi"]
