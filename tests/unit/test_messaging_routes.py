"""HTTP-level tests for the remote conversational channels.

Exercises the remote-URL token auth + dispatch and the Slack signature gate +
url_verification handshake through a real FastAPI app, with the chat agent
stubbed so no LLM is called.
"""

from __future__ import annotations

import hashlib
import hmac
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import devai.chat.remote_routes as remote_routes
import devai.chat.slack_routes as slack_routes
from devai.adapters.messaging.base import ConversationReply, ConversationTurn


class _FakeChannel:
    name = "remote_url"

    async def dispatch(self, raw):
        return ConversationReply(text=f"echo:{raw['text']}")


class _FakeSlackChannel:
    name = "slack"

    async def to_turn(self, payload):
        ev = payload.get("event", {})
        ts = ev.get("thread_ts") or ev.get("ts")
        return ConversationTurn(
            text=ev.get("text", ""),
            conversation_id=f'slack:{ev.get("channel")}:{ts}',
            channel="slack",
            metadata={"channel_id": ev.get("channel"), "thread_ts": ts},
        )


class _FakeSvc:
    def __init__(self) -> None:
        self.channels = {"remote_url": _FakeChannel(), "slack": _FakeSlackChannel()}
        self.enqueued = []

    async def dispatch_inline(self, name, raw):
        return await self.channels[name].dispatch(raw)

    async def enqueue_turn(self, turn) -> None:
        self.enqueued.append(turn)


class _Cfg:
    remote_chat_api_token = "secret-token"
    slack_signing_secret = "slack-sek"


class _Redis:
    async def set(self, *a, **k):
        return True  # always "fresh" — not a duplicate


class _State:
    redis = _Redis()


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(remote_routes.router)
    app.include_router(slack_routes.router)
    app.state.config = _Cfg()
    app.state.state_manager = _State()
    app.state.messaging_service = _FakeSvc()
    return app


@pytest.fixture()
def client() -> TestClient:
    return TestClient(_app())


# ---- remote URL auth + dispatch ----


def test_remote_requires_token(client: TestClient) -> None:
    r = client.post("/remote/threads/t1/messages", json={"text": "hi"})
    assert r.status_code == 401


def test_remote_rejects_wrong_token(client: TestClient) -> None:
    r = client.post(
        "/remote/threads/t1/messages",
        json={"text": "hi"},
        headers={"Authorization": "Bearer nope"},
    )
    assert r.status_code == 401


def test_remote_happy_path(client: TestClient) -> None:
    r = client.post(
        "/remote/threads/t1/messages",
        json={"text": "why did it fail"},
        headers={"Authorization": "Bearer secret-token"},
    )
    assert r.status_code == 200
    assert r.json() == {"reply": "echo:why did it fail", "thread_id": "t1"}


def test_remote_blank_message_is_400(client: TestClient) -> None:
    r = client.post(
        "/remote/threads/t1/messages",
        json={"text": "   "},
        headers={"Authorization": "Bearer secret-token"},
    )
    assert r.status_code == 400


# ---- slack signature + handshake ----


def _sign(secret: str, body: bytes) -> dict[str, str]:
    ts = str(int(time.time()))
    base = b"v0:" + ts.encode() + b":" + body
    sig = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return {"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig}


def test_slack_rejects_bad_signature(client: TestClient) -> None:
    r = client.post(
        "/webhook/slack",
        content=b'{"type":"event_callback"}',
        headers={"X-Slack-Request-Timestamp": "1", "X-Slack-Signature": "v0=bad"},
    )
    assert r.status_code == 401


def test_slack_url_verification(client: TestClient) -> None:
    body = b'{"type":"url_verification","challenge":"abc123"}'
    r = client.post("/webhook/slack", content=body, headers=_sign("slack-sek", body))
    assert r.status_code == 200
    assert r.text == "abc123"


def test_slack_event_enqueues_and_acks(client: TestClient) -> None:
    body = (
        b'{"type":"event_callback","event_id":"Ev1","team_id":"T1",'
        b'"event":{"type":"app_mention","text":"<@U1> hi","channel":"C1","ts":"1.0","user":"U2"}}'
    )
    r = client.post("/webhook/slack", content=body, headers=_sign("slack-sek", body))
    assert r.status_code == 200
    assert r.json()["status"] == "accepted"
    svc = client.app.state.messaging_service
    assert len(svc.enqueued) == 1
    assert svc.enqueued[0].conversation_id == "slack:C1:1.0"
