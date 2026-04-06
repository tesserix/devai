"""Chat API routes — WebSocket and REST endpoints for the DevAI chatbot."""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("/api/message")
async def chat_message(request: Any) -> dict[str, str]:
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

    agent = DevAIChatAgent(config, state, database=db)
    response = await agent.chat(message, session_id)

    return {"response": response, "session_id": session_id}


@router.post("/api/message/stream")
async def chat_stream(request: Any) -> StreamingResponse:
    """Stream a chat response token by token via SSE."""
    body = await request.json()
    message = body.get("message", "")
    session_id = body.get("session_id", "default")

    from devai.chat.agent import DevAIChatAgent

    config = request.app.state.config
    state = request.app.state.state_manager
    db = getattr(request.app.state, "database", None)

    agent = DevAIChatAgent(config, state, database=db)

    async def event_stream():
        async for chunk in agent.stream_chat(message, session_id):
            yield f"data: {json.dumps({'text': chunk})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.websocket("/ws")
async def chat_websocket(websocket: WebSocket) -> None:
    """WebSocket endpoint for real-time chat."""
    await websocket.accept()
    session_id = "ws-" + str(id(websocket))

    from devai.chat.agent import DevAIChatAgent

    config = websocket.app.state.config
    state = websocket.app.state.state_manager
    db = getattr(websocket.app.state, "database", None)

    agent = DevAIChatAgent(config, state, database=db)

    try:
        while True:
            data = await websocket.receive_text()
            msg = json.loads(data)
            user_message = msg.get("message", "")

            if not user_message:
                continue

            # Stream response chunks
            async for chunk in agent.stream_chat(user_message, session_id):
                await websocket.send_json({"type": "chunk", "text": chunk})

            await websocket.send_json({"type": "done"})

    except WebSocketDisconnect:
        agent.clear_session(session_id)
