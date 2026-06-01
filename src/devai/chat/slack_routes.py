"""Slack Events API route — POST /webhook/slack.

Verifies the Slack request signature, handles the ``url_verification`` handshake,
de-dupes retried deliveries, and hands real events to the MessagingService
worker so we ack within Slack's 3-second budget. The worker posts the reply
back in-thread.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from devai.adapters.messaging.slack import verify_slack_signature

logger = logging.getLogger(__name__)

router = APIRouter(tags=["slack"])

_DEDUP_TTL_SECONDS = 600  # ignore the same Slack event_id for 10 minutes


@router.post("/webhook/slack")
async def slack_events(request: Request):
    """Receive Slack Events API callbacks."""
    raw_body = await request.body()
    config = request.app.state.config

    # 1. Signature verification (fail closed when a signing secret is set).
    signing_secret = getattr(config, "slack_signing_secret", "") or ""
    if signing_secret:
        ts = request.headers.get("x-slack-request-timestamp", "")
        sig = request.headers.get("x-slack-signature", "")
        if not verify_slack_signature(signing_secret, ts, sig, raw_body):
            return JSONResponse({"error": "invalid signature"}, status_code=401)

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "bad payload"}, status_code=400)

    # 2. URL verification handshake (Slack sends this when you register the URL).
    if payload.get("type") == "url_verification":
        return PlainTextResponse(payload.get("challenge", ""))

    svc = getattr(request.app.state, "messaging_service", None)
    if svc is None or "slack" not in svc.channels:
        # Ack so Slack doesn't retry; channel just isn't enabled.
        return JSONResponse({"status": "ignored"})

    slack_channel = svc.channels["slack"]

    # 3. De-dupe retried deliveries on event_id (Redis SETNX, short TTL).
    event_id = payload.get("event_id", "")
    if event_id:
        try:
            state = request.app.state.state_manager
            fresh = await state.redis.set(
                f"devai:slack:event:{event_id}", "1", nx=True, ex=_DEDUP_TTL_SECONDS
            )
            if not fresh:
                return JSONResponse({"status": "duplicate"})
        except Exception:
            logger.debug("slack dedup check failed — proceeding", exc_info=True)

    # 4. Parse → enqueue on the worker → ack immediately (within 3s).
    turn = await slack_channel.to_turn(payload)
    if turn is not None:
        await svc.enqueue_turn(turn)

    return JSONResponse({"status": "accepted"})


__all__ = ["router"]
