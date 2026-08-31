"""Request body size caps and per-principal rate limiting."""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from devai.services.guardrails import RateLimiter
from devai.services.request_limits import (
    MAX_API_BODY_BYTES,
    MAX_WEBHOOK_BODY_BYTES,
    BodySizeLimitMiddleware,
    enforce_rate_limit,
    max_body_bytes,
)


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(BodySizeLimitMiddleware)

    @app.post("/api/echo")
    async def echo(request: Request):
        return {"size": len(await request.body())}

    @app.post("/webhook/github")
    async def hook(request: Request):
        return {"size": len(await request.body())}

    @app.post("/api/stream")
    async def stream():
        async def chunks():
            await asyncio.sleep(0.01)
            yield b"stream-ready"

        return StreamingResponse(chunks(), media_type="text/plain")

    return app


def test_api_body_cap_is_tighter_than_webhook():
    assert max_body_bytes("/api/echo") == MAX_API_BODY_BYTES
    assert max_body_bytes("/webhook/github") == MAX_WEBHOOK_BODY_BYTES
    assert MAX_API_BODY_BYTES < MAX_WEBHOOK_BODY_BYTES


def test_small_body_passes():
    client = TestClient(_app())
    r = client.post("/api/echo", content=b"x" * 1000)
    assert r.status_code == 200
    assert r.json()["size"] == 1000


def test_buffered_request_does_not_disconnect_streaming_response():
    client = TestClient(_app())

    response = client.post("/api/stream", content=b"request")

    assert response.status_code == 200
    assert response.text == "stream-ready"


def test_oversized_api_body_is_rejected():
    client = TestClient(_app())
    r = client.post("/api/echo", content=b"x" * (MAX_API_BODY_BYTES + 1))
    assert r.status_code == 413


def test_oversized_body_rejected_without_content_length():
    """A chunked upload has no Content-Length, so the cap must count bytes."""

    def chunks():
        for _ in range((MAX_API_BODY_BYTES // 1000) + 2):
            yield b"x" * 1000

    client = TestClient(_app())
    r = client.post("/api/echo", content=chunks())
    assert r.status_code == 413


def test_webhook_allows_a_payload_over_the_api_cap():
    client = TestClient(_app())
    r = client.post("/webhook/github", content=b"x" * (MAX_API_BODY_BYTES + 1000))
    assert r.status_code == 200


class _FakePipeline:
    def __init__(self, store):
        self.store = store
        self.ops = []

    def zremrangebyscore(self, key, lo, hi):
        self.ops.append(("trim", key))

    def zcard(self, key):
        self.ops.append(("card", key))

    def zadd(self, key, mapping):
        self.ops.append(("add", key, mapping))

    def expire(self, key, ttl):
        self.ops.append(("expire", key))

    async def execute(self):
        results = []
        for op in self.ops:
            if op[0] == "card":
                results.append(len(self.store.get(op[1], [])))
            elif op[0] == "add":
                self.store.setdefault(op[1], []).extend(op[2])
                results.append(1)
            else:
                results.append(1)
        return results


class _FakeRedis:
    def __init__(self):
        self.store = {}

    def pipeline(self):
        return _FakePipeline(self.store)


@pytest.mark.asyncio
async def test_rate_limit_is_scoped_per_subject():
    limiter = RateLimiter(_FakeRedis())
    for _ in range(5):
        assert await limiter.acquire("pipeline_trigger", subject="alice@x.io")
    assert not await limiter.acquire("pipeline_trigger", subject="alice@x.io")
    # A different caller has their own budget.
    assert await limiter.acquire("pipeline_trigger", subject="bob@x.io")


@pytest.mark.asyncio
async def test_rate_limit_without_subject_keeps_global_key():
    redis = _FakeRedis()
    limiter = RateLimiter(redis)
    await limiter.acquire("github_api")
    assert "devai:ratelimit:github_api" in redis.store


def _limited_app(redis):
    app = FastAPI()
    app.state.state_manager = SimpleNamespace(redis=redis)

    @app.post("/trigger")
    async def trigger(request: Request):
        principal = SimpleNamespace(email=request.headers.get("x-user", ""), uid="")
        await enforce_rate_limit(request, "pipeline_trigger", principal)
        return {"ok": True}

    return TestClient(app)


def test_over_limit_caller_gets_429_and_others_are_unaffected():
    client = _limited_app(_FakeRedis())
    for _ in range(5):
        assert client.post("/trigger", headers={"x-user": "alice@x.io"}).status_code == 200
    assert client.post("/trigger", headers={"x-user": "alice@x.io"}).status_code == 429
    assert client.post("/trigger", headers={"x-user": "bob@x.io"}).status_code == 200


def test_rate_limit_fails_open_when_redis_is_down():
    class _Broken:
        def pipeline(self):
            raise ConnectionError("redis down")

    client = _limited_app(_Broken())
    assert client.post("/trigger", headers={"x-user": "alice@x.io"}).status_code == 200
