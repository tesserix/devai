from __future__ import annotations

from types import SimpleNamespace

import pytest

from devai.admin.service import active_user_totals, active_users_timeseries, signin_count


class _Pool:
    def __init__(self, rows):
        self.rows = rows
        self.queries: list[tuple] = []

    async def fetch(self, sql, *args):
        self.queries.append((sql, args))
        return self.rows


class _BrokenPool:
    async def fetch(self, *_a):
        raise RuntimeError("db down")


def _db(pool):
    return SimpleNamespace(pool=pool)


@pytest.mark.asyncio
async def test_timeseries_maps_rows():
    pool = _Pool([{"date": "2026-08-27", "users": 3}, {"date": "2026-08-28", "users": 5}])
    out = await active_users_timeseries(_db(pool), 30)
    assert out == [{"date": "2026-08-27", "users": 3}, {"date": "2026-08-28", "users": 5}]


@pytest.mark.asyncio
async def test_timeseries_passes_the_day_window():
    pool = _Pool([])
    await active_users_timeseries(_db(pool), 7)
    assert pool.queries[0][1] == (7,)


@pytest.mark.asyncio
async def test_signin_count_reads_the_scalar():
    pool = _Pool([{"count": 12}])
    assert await signin_count(_db(pool), 30) == 12


@pytest.mark.asyncio
async def test_signin_count_with_no_rows_is_zero():
    assert await signin_count(_db(_Pool([])), 30) == 0


@pytest.mark.asyncio
async def test_user_totals_maps_rows():
    pool = _Pool([{"user": "a@example.com", "days_active": 4, "last_seen": "2026-08-29"}])
    out = await active_user_totals(_db(pool), 30)
    assert out == [{"user": "a@example.com", "days_active": 4, "last_seen": "2026-08-29"}]


@pytest.mark.asyncio
async def test_missing_database_degrades_to_empty():
    assert await active_users_timeseries(None, 30) == []
    assert await signin_count(None, 30) == 0
    assert await active_user_totals(None, 30) == []


@pytest.mark.asyncio
async def test_query_failure_degrades_to_empty():
    db = _db(_BrokenPool())
    assert await active_users_timeseries(db, 30) == []
    assert await signin_count(db, 30) == 0
    assert await active_user_totals(db, 30) == []
