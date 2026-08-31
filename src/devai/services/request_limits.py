"""Request body size caps and per-caller rate limits.

Starlette's ``request.body()`` reads whatever the client sends, so any
unauthenticated POST is a memory-exhaustion primitive. This middleware bounds
the read before the route sees it: 25 MiB on webhook paths (GitHub's own
payload ceiling, so nothing that works today starts failing) and 1 MiB on
everything else.

The rate-limit helper attaches the existing guardrails limiter to a request,
keyed on the caller so one user cannot spend a per-user budget for everyone.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import HTTPException
from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from fastapi import Request
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

    from devai.identity import Principal

logger = logging.getLogger(__name__)

MAX_API_BODY_BYTES = 1_048_576
MAX_WEBHOOK_BODY_BYTES = 26_214_400

_WEBHOOK_PREFIX = "/webhook/"


def max_body_bytes(path: str) -> int:
    """Byte cap for a request path."""
    return MAX_WEBHOOK_BODY_BYTES if path.startswith(_WEBHOOK_PREFIX) else MAX_API_BODY_BYTES


class BodySizeLimitMiddleware:
    """Reject oversized request bodies with 413 before routing."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        limit = max_body_bytes(scope.get("path", ""))

        declared = _content_length(scope)
        if declared is not None and declared > limit:
            await self._reject(scope, receive, send, limit)
            return

        body, overflowed = await _read_capped(receive, limit)
        if overflowed:
            await self._reject(scope, receive, send, limit)
            return

        await self.app(scope, _replay(body, receive), send)

    async def _reject(self, scope: Scope, receive: Receive, send: Send, limit: int) -> None:
        logger.warning("Request body over %d bytes rejected on %s", limit, scope.get("path", ""))
        response = JSONResponse({"detail": "Request body too large"}, status_code=413)
        await response(scope, _replay(b"", receive), send)


def _content_length(scope: Scope) -> int | None:
    for name, value in scope.get("headers", []):
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


async def _read_capped(receive: Receive, limit: int) -> tuple[bytes, bool]:
    """Buffer the body, stopping as soon as it exceeds ``limit``."""
    chunks: list[bytes] = []
    size = 0
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return b"".join(chunks), False
        body = message.get("body", b"")
        size += len(body)
        if size > limit:
            return b"", True
        chunks.append(body)
        if not message.get("more_body", False):
            return b"".join(chunks), False


def _subject(request: Request, principal: Principal | None) -> str:
    """Who the budget belongs to — the authenticated caller, else their IP."""
    if principal is not None:
        who = getattr(principal, "email", "") or getattr(principal, "uid", "")
        if who:
            return who
    return request.client.host if request.client else "anonymous"


async def enforce_rate_limit(
    request: Request,
    resource: str,
    principal: Principal | None = None,
) -> None:
    """Raise 429 if this caller is over the limit for ``resource``.

    Fails open: a Redis outage must not take the whole API down with it.
    """
    from devai.services.guardrails import RateLimiter

    redis = getattr(getattr(request.app.state, "state_manager", None), "redis", None)
    if redis is None:
        return
    try:
        allowed = await RateLimiter(redis).acquire(resource, subject=_subject(request, principal))
    except Exception:  # noqa: BLE001
        logger.warning("Rate limit check failed for %s — allowing request", resource, exc_info=True)
        return
    if not allowed:
        raise HTTPException(status_code=429, detail="Rate limit exceeded — try again shortly")


def _replay(body: bytes, receive: Receive) -> Receive:
    sent = False

    async def replay() -> Message:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await receive()

    return replay
