from __future__ import annotations

from types import SimpleNamespace

import pytest

from devai.admin.activity import ACTION_ACTIVE, record_active
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
