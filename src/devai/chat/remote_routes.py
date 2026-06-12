"""Remote conversational routes — talk to DevAI from any remote app/URL.

A small, embeddable HTTP thread API that sits on the shared ConversationGateway
(via ``app.state.messaging_service``). Two endpoints:

    POST /remote/threads/{thread_id}/messages   -> {"reply": "..."}   (sync)
    GET  /remote/threads/{thread_id}/stream?... -> text/event-stream   (tokens)

Both are guarded by a shared static bearer token (``DEVAI_REMOTE_CHAT_API_TOKEN``)
when one is configured; an empty token means open (local/dev only). The token
caller is attributed as a ``Principal`` so audit + A2A still record "who".
"""

from __future__ import annotations

import hmac
import json
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from devai.identity import Principal, trace_id_from_request

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/remote", tags=["remote-chat"])


def _check_token(request: Request) -> Principal:
    """Validate the static bearer token; return the attributed Principal.

    Constant-time compare. Empty configured token = auth disabled (returns a
    generic remote principal) — intended for local/dev only.
    """
    config = request.app.state.config
    expected = getattr(config, "remote_chat_api_token", "") or ""

    if not expected:
        return Principal(
            email="remote@devai",
            auth_provider="remote-open",
            display_name="remote (no auth)",
        )

    auth = request.headers.get("authorization", "")
    presented = auth[7:] if auth.lower().startswith("bearer ") else ""
    if not presented or not hmac.compare_digest(presented, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing API token")

    # Optional caller hint for attribution; does not grant access.
    who = request.headers.get("x-devai-user", "") or "remote-api"
    return Principal(email=f"{who}", uid=who, auth_provider="remote-token", display_name=who)


@router.post("/threads/{thread_id}/messages")
async def post_thread_message(thread_id: str, request: Request) -> dict[str, str]:
    """Send one message to a thread and get the agent's reply synchronously."""
    principal = _check_token(request)
    svc = getattr(request.app.state, "messaging_service", None)
    if svc is None or "remote_url" not in svc.channels:
        raise HTTPException(status_code=503, detail="Remote chat is not enabled")

    body = await request.json()
    text = (body.get("text") or body.get("message") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Field 'text' is required")

    raw = {
        "text": text,
        "thread_id": thread_id,
        "principal": principal,
        "trace_id": trace_id_from_request(request),
        "user_id": principal.uid or principal.email,
    }
    reply = await svc.dispatch_inline("remote_url", raw)
    return {"reply": reply.text if reply else "", "thread_id": thread_id}


@router.get("/threads/{thread_id}/stream")
async def stream_thread_message(thread_id: str, request: Request) -> StreamingResponse:
    """Stream a reply token-by-token via SSE for a remote thread.

    The message is passed as the ``text`` query param so plain ``EventSource``
    clients (which can't send a body) work. Reuses the chat agent's streaming.
    """
    principal = _check_token(request)

    text = (request.query_params.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Query param 'text' is required")

    # Same policy point as every chat transport: per-user overlay agent
    # (their own LLM creds) + trial enforcement, via the gateway.
    from devai.chat.routes import _resolve_chat

    agent, trial_block = await _resolve_chat(request.app.state, principal)
    trace_id = trace_id_from_request(request)
    session_id = f"url:{thread_id}"

    async def event_stream():
        if agent is None:
            yield f"data: {json.dumps({'text': trial_block})}\n\n"
            yield "data: [DONE]\n\n"
            return
        try:
            async for chunk in agent.stream_chat(text, session_id, principal=principal, trace_id=trace_id):
                yield f"data: {json.dumps({'text': chunk})}\n\n"
        except Exception as exc:  # never break the stream mid-flight
            logger.exception("remote stream failed for thread %s", thread_id)
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


__all__ = ["router"]
