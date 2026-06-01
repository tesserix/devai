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

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("/api/message")
async def chat_message(request: Request) -> dict[str, str]:
    """Send a message to the chat agent and get a response."""
    body = await request.json()
    message = body.get("message", "")
    session_id = body.get("session_id", "default")

    if not message:
        return {"response": "Please send a message."}

    from devai.chat.agent import DevAIChatAgent

    config = request.app.state.config
    state = request.app.state.state_manager
    db = getattr(request.app.state, "database", None)

    principal = await extract_principal(request) or Principal.system()
    trace_id = trace_id_from_request(request)

    if not hasattr(request.app.state, "chat_agent"):
        request.app.state.chat_agent = DevAIChatAgent(config, state, database=db)
    agent = request.app.state.chat_agent
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

    from devai.chat.agent import DevAIChatAgent

    config = request.app.state.config
    state = request.app.state.state_manager
    db = getattr(request.app.state, "database", None)

    principal = await extract_principal(request) or Principal.system()
    trace_id = trace_id_from_request(request)

    if not hasattr(request.app.state, "chat_agent"):
        request.app.state.chat_agent = DevAIChatAgent(config, state, database=db)
    agent = request.app.state.chat_agent

    async def event_stream():
        async for chunk in agent.stream_chat(message, session_id, principal=principal, trace_id=trace_id):
            yield f"data: {json.dumps({'text': chunk})}\n\n"
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

    from devai.chat.agent import DevAIChatAgent

    config = websocket.app.state.config
    state = websocket.app.state.state_manager
    db = getattr(websocket.app.state, "database", None)

    # Resolve principal from the upgrade request. WebSocket exposes
    # headers/cookies through the same API as Request — we duck-type
    # rather than importing Request to keep the surface small.
    principal = await extract_principal(websocket) or Principal.system()  # type: ignore[arg-type]
    trace_id = trace_id_from_request(websocket)  # type: ignore[arg-type]

    if not hasattr(websocket.app.state, "chat_agent"):
        websocket.app.state.chat_agent = DevAIChatAgent(config, state, database=db)
    agent = websocket.app.state.chat_agent

    try:
        while True:
            data = await websocket.receive_text()
            msg = json.loads(data)
            user_message = msg.get("message", "")

            if not user_message:
                continue

            # Stream response chunks
            async for chunk in agent.stream_chat(user_message, session_id, principal=principal, trace_id=trace_id):
                await websocket.send_json({"type": "chunk", "text": chunk})

            await websocket.send_json({"type": "done"})

    except WebSocketDisconnect:
        agent.clear_session(session_id)
