"""PostgresOnboardingStore resilience: retry when the mesh drops a pooled
connection (ambient ztunnel resetting an idle TCP connection surfaces as
asyncpg ConnectionDoesNotExistError). A single reset must not 502 the Repos
page — the store retries on a fresh connection."""

from __future__ import annotations

import asyncpg
import pytest

from devai.onboarding.store import PostgresOnboardingStore


class _FlakyPool:
    """Fake asyncpg pool: the first call to each method raises a dropped-
    connection error, the next succeeds — mimicking a pool that handed out a
    reset connection then dialed a fresh one."""

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows
        self.fetch_calls = 0

    async def fetch(self, *args: object) -> list[dict]:
        self.fetch_calls += 1
        if self.fetch_calls == 1:
            raise asyncpg.exceptions.ConnectionDoesNotExistError("connection was closed")
        return self._rows


@pytest.mark.asyncio
async def test_list_retries_on_dropped_connection() -> None:
    pool = _FlakyPool([{"owner": "tesserix", "name": "devai", "state": "onboarded"}])
    store = PostgresOnboardingStore(pool)

    rows = await store.list()

    assert pool.fetch_calls == 2  # first attempt reset, retry succeeded
    assert [r.full_name for r in rows] == ["tesserix/devai"]


@pytest.mark.asyncio
async def test_call_gives_up_after_retries() -> None:
    class _DeadPool:
        async def fetch(self, *args: object) -> list[dict]:
            raise asyncpg.exceptions.ConnectionDoesNotExistError("still down")

    store = PostgresOnboardingStore(_DeadPool())
    # A persistently dead pool still surfaces the error (no infinite loop) so
    # the route returns a clean 5xx rather than hanging.
    with pytest.raises(asyncpg.exceptions.ConnectionDoesNotExistError):
        await store.list()
