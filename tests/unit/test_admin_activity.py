from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from devai.admin.activity import ACTION_ACTIVE, ActivityMiddleware, record_active
from devai.identity import Principal


class _Database:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    async def audit(self, action, actor, actor_type="agent", details=None, **_kw):
        self.rows.append({"action": action, "actor": actor, "actor_type": actor_type, "details": details})


class _Redis:
    """Minimal SET NX EX stand-in — the real dedup guard."""

    def __init__(self) -> None:
        self.keys: dict[str, str] = {}

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.keys:
            return None
        self.keys[key] = value
        return True


class _BrokenRedis:
    async def set(self, *_a, **_kw):
        raise RuntimeError("redis down")


def _state(db, redis):
    return SimpleNamespace(analytics_db=db, activity_redis=redis)


def _principal() -> Principal:
    return Principal(email="user@example.com", uid="u-1", tenant_id="t-1")


@pytest.mark.asyncio
async def test_records_one_row_for_a_user():
    db, state = _Database(), None
    state = _state(db, _Redis())
    assert await record_active(state, _principal()) is True
    assert len(db.rows) == 1
    assert db.rows[0]["action"] == ACTION_ACTIVE
    assert db.rows[0]["actor"] == "user@example.com"
    assert db.rows[0]["actor_type"] == "user"


@pytest.mark.asyncio
async def test_second_request_same_day_is_deduplicated():
    db = _Database()
    state = _state(db, _Redis())
    assert await record_active(state, _principal()) is True
    assert await record_active(state, _principal()) is False
    assert len(db.rows) == 1


@pytest.mark.asyncio
async def test_distinct_users_each_get_a_row():
    db = _Database()
    state = _state(db, _Redis())
    await record_active(state, Principal(email="a@example.com", uid="a"))
    await record_active(state, Principal(email="b@example.com", uid="b"))
    assert len(db.rows) == 2


@pytest.mark.asyncio
async def test_redis_failure_does_not_raise():
    db = _Database()
    state = _state(db, _BrokenRedis())
    assert await record_active(state, _principal()) is False
    assert db.rows == []


@pytest.mark.asyncio
async def test_missing_database_does_not_raise():
    state = _state(None, _Redis())
    assert await record_active(state, _principal()) is False


@pytest.mark.asyncio
async def test_anonymous_principal_is_ignored():
    db = _Database()
    state = _state(db, _Redis())
    assert await record_active(state, None) is False
    assert db.rows == []


@pytest.mark.asyncio
async def test_system_principal_is_ignored():
    db = _Database()
    state = _state(db, _Redis())
    assert await record_active(state, Principal.system()) is False
    assert db.rows == []


@pytest.mark.asyncio
async def test_webhook_principal_is_ignored():
    db = _Database()
    state = _state(db, _Redis())
    principal = Principal.webhook(provider="github", sender_login="octocat")
    assert await record_active(state, principal) is False
    assert db.rows == []


def _middleware_client(db, redis, principal, monkeypatch) -> TestClient:
    app = FastAPI()
    app.add_middleware(ActivityMiddleware)
    app.state.analytics_db = db
    app.state.activity_redis = redis

    @app.get("/api/thing")
    async def _thing():
        return {"ok": True}

    @app.get("/healthz")
    async def _health():
        return {"ok": True}

    async def _extract(_request):
        return principal

    import devai.identity as identity

    monkeypatch.setattr(identity, "extract_principal", _extract)
    return TestClient(app)


def test_middleware_records_and_passes_through_response(monkeypatch):
    db = _Database()
    client = _middleware_client(db, _Redis(), _principal(), monkeypatch)
    res = client.get("/api/thing")
    assert res.status_code == 200
    assert res.json() == {"ok": True}
    assert len(db.rows) == 1
    assert db.rows[0]["actor"] == "user@example.com"


def test_middleware_survives_recording_failure(monkeypatch):
    client = _middleware_client(None, _BrokenRedis(), _principal(), monkeypatch)
    res = client.get("/api/thing")
    assert res.status_code == 200
    assert res.json() == {"ok": True}


def test_middleware_skips_recording_on_skip_prefix(monkeypatch):
    db = _Database()
    client = _middleware_client(db, _Redis(), _principal(), monkeypatch)
    res = client.get("/healthz")
    assert res.status_code == 200
    assert db.rows == []


def test_middleware_skips_recording_for_anonymous(monkeypatch):
    db = _Database()
    client = _middleware_client(db, _Redis(), None, monkeypatch)
    res = client.get("/api/thing")
    assert res.status_code == 200
    assert db.rows == []


@pytest.mark.asyncio
async def test_record_login_writes_a_row():
    from devai.admin.activity import ACTION_LOGIN, record_login

    db = _Database()
    state = SimpleNamespace(analytics_db=db)
    assert await record_login(state, "user@example.com") is True
    assert db.rows[0]["action"] == ACTION_LOGIN
    assert db.rows[0]["actor_type"] == "user"


@pytest.mark.asyncio
async def test_record_login_without_database_is_silent():
    from devai.admin.activity import record_login

    assert await record_login(SimpleNamespace(analytics_db=None), "u@example.com") is False


@pytest.mark.asyncio
async def test_record_login_is_not_deduplicated():
    """Unlike active-days, every sign-in is its own event."""
    from devai.admin.activity import record_login

    db = _Database()
    state = SimpleNamespace(analytics_db=db)
    await record_login(state, "user@example.com")
    await record_login(state, "user@example.com")
    assert len(db.rows) == 2


@pytest.mark.asyncio
async def test_record_login_survives_a_failing_database():
    """A sign-in must never be blocked by the analytics write behind it."""
    from devai.admin.activity import record_login

    class _BrokenDatabase:
        async def audit(self, *_a, **_kw):
            raise RuntimeError("postgres down")

    state = SimpleNamespace(analytics_db=_BrokenDatabase())

    assert await record_login(state, "user@example.com") is False
