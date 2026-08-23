"""Chat API routes — WebSocket and REST endpoints for the DevAI chatbot.

Every entrypoint resolves the caller's identity (via auth-bff headers or
the dashboard's session cookie) and threads it into the chat agent.
That's how injection-style tools like ``inject_pipeline_requirements``
end up with a real user attached to the resulting A2A messages instead
of the literal string ``"human"``.
"""

import json
import logging

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

from devai.identity import Principal, extract_principal, trace_id_from_request
from devai.services.request_limits import enforce_rate_limit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])


def _gateway(state) -> "object":
    """The app-wide ConversationGateway (lazily created) — the single
    policy point for chat: per-user overlay agents (their own LLM creds)
    + trial enforcement. Every chat transport resolves its agent here so
    no endpoint can hand a user the raw shared-key agent."""
    gw = getattr(state, "chat_gateway", None)
    if gw is None:
        from devai.chat.gateway import ConversationGateway

        gw = ConversationGateway(
            state.config,
            state.state_manager,
            database=getattr(state, "database", None),
            settings_service=getattr(state, "settings_service", None),
        )
        state.chat_gateway = gw
    return gw


async def _resolve_chat(state, principal: Principal):
    """(agent, trial_block_text) for this principal. agent is None only
    when the trial budget is spent — return the block text verbatim."""
    gw = _gateway(state)
    block = await gw.trial_guard(principal)
    if block is not None:
        return None, block.text
    return await gw.agent_for(principal), None


@router.post("/api/message")
async def chat_message(request: Request) -> dict[str, str]:
    """Send a message to the chat agent and get a response."""
    body = await request.json()
    message = body.get("message", "")
    session_id = body.get("session_id", "default")

    if not message:
        return {"response": "Please send a message."}

    principal = await extract_principal(request) or Principal.system()
    await enforce_rate_limit(request, "chat_message", principal)
    trace_id = trace_id_from_request(request)

    agent, trial_block = await _resolve_chat(request.app.state, principal)
    if agent is None:
        return {"response": trial_block, "session_id": session_id}
    try:
        response = await agent.chat(message, session_id, principal=principal, trace_id=trace_id)
    except Exception as exc:
        logger.exception("Chat failed for session %s (user=%s)", session_id, principal.email)
        # Don't echo raw exception text to the client — it can carry
        # connection strings or tokens. The full error is in the logs.
        from devai.services.redact import redact_secrets

        response = f"Sorry, I encountered an error: {redact_secrets(str(exc))[:200]}"

    return {"response": response, "session_id": session_id}


@router.post("/api/message/stream")
async def chat_stream(request: Request) -> StreamingResponse:
    """Stream a chat response token by token via SSE."""
    body = await request.json()
    message = body.get("message", "")
    session_id = body.get("session_id", "default")

    principal = await extract_principal(request) or Principal.system()
    await enforce_rate_limit(request, "chat_message", principal)
    trace_id = trace_id_from_request(request)

    agent, trial_block = await _resolve_chat(request.app.state, principal)

    async def event_stream():
        from devai.services.redact import redact_secrets

        if agent is None:
            yield f"data: {json.dumps({'text': trial_block})}\n\n"
            yield "data: [DONE]\n\n"
            return
        try:
            async for chunk in agent.stream_chat(message, session_id, principal=principal, trace_id=trace_id):
                # Redact each streamed chunk — tool output / error text can
                # carry connection strings or bare provider keys.
                yield f"data: {json.dumps({'text': redact_secrets(chunk)})}\n\n"
        except Exception as exc:  # noqa: BLE001
            logger.exception("Chat stream failed for session %s (user=%s)", session_id, principal.email)
            safe = redact_secrets(str(exc))[:200]
            yield f"data: {json.dumps({'text': f'Sorry, I encountered an error: {safe}'})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.websocket("/ws")
async def chat_websocket(websocket: WebSocket) -> None:
    """WebSocket endpoint for real-time chat.

    Identity is resolved once at connect time from the upgrade request's
    headers / cookies. WebSocket upgrades carry the same auth-bff
    headers as a normal HTTP request, so the principal we resolve here
    is good for the lifetime of the socket.
    """
    await websocket.accept()
    session_id = "ws-" + str(id(websocket))

    # Resolve principal from the upgrade request. WebSocket exposes
    # headers/cookies through the same API as Request — we duck-type
    # rather than importing Request to keep the surface small.
    principal = await extract_principal(websocket) or Principal.system()  # type: ignore[arg-type]
    trace_id = trace_id_from_request(websocket)  # type: ignore[arg-type]

    agent, trial_block = await _resolve_chat(websocket.app.state, principal)
    if agent is None:
        await websocket.send_json({"type": "error", "text": trial_block})
        await websocket.close()
        return

    from devai.services.redact import redact_secrets

    try:
        while True:
            data = await websocket.receive_text()
            msg = json.loads(data)
            user_message = msg.get("message", "")

            if not user_message:
                continue

            # Stream response chunks, redacting each — tool output / error
            # text can carry connection strings or bare provider keys.
            try:
                async for chunk in agent.stream_chat(user_message, session_id, principal=principal, trace_id=trace_id):
                    await websocket.send_json({"type": "chunk", "text": redact_secrets(chunk)})
            except Exception as exc:  # noqa: BLE001
                logger.exception("Chat WS turn failed for session %s (user=%s)", session_id, principal.email)
                safe = redact_secrets(str(exc))[:200]
                await websocket.send_json({"type": "chunk", "text": f"Sorry, I encountered an error: {safe}"})

            await websocket.send_json({"type": "done"})

    except WebSocketDisconnect:
        agent.clear_session(session_id, principal=principal)
