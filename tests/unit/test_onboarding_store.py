"""InMemoryOnboardingStore contract tests.

The Postgres store runs the same SQL through asyncpg; this suite proves
the behavioural contract the service relies on without needing a DB.
"""

from __future__ import annotations

import pytest

from devai.onboarding.models import OnboardedRepo, OnboardingState
from devai.onboarding.store import InMemoryOnboardingStore


@pytest.fixture
def store() -> InMemoryOnboardingStore:
    return InMemoryOnboardingStore()


@pytest.mark.asyncio
async def test_upsert_then_get_roundtrips(store) -> None:
    await store.upsert(OnboardedRepo(owner="tesserix", name="devai", state=OnboardingState.PENDING_PR))
    row = await store.get("tesserix", "devai")
    assert row is not None
    assert row.state == OnboardingState.PENDING_PR
    assert row.created_at is not None
    assert row.updated_at is not None


@pytest.mark.asyncio
async def test_upsert_preserves_created_at_on_update(store) -> None:
    first = await store.upsert(OnboardedRepo(owner="tesserix", name="devai"))
    created = first.created_at
    updated = await store.upsert(
        OnboardedRepo(owner="tesserix", name="devai", state=OnboardingState.ONBOARDED)
    )
    assert updated.created_at == created
    assert updated.state == OnboardingState.ONBOARDED


@pytest.mark.asyncio
async def test_get_missing_returns_none(store) -> None:
    assert await store.get("tesserix", "nope") is None


@pytest.mark.asyncio
async def test_list_filters_by_state(store) -> None:
    await store.upsert(OnboardedRepo(owner="o", name="a", state=OnboardingState.ONBOARDED))
    await store.upsert(OnboardedRepo(owner="o", name="b", state=OnboardingState.PENDING_PR))
    onboarded = await store.list(state=OnboardingState.ONBOARDED)
    assert {r.name for r in onboarded} == {"a"}


@pytest.mark.asyncio
async def test_list_excludes_archived_by_default(store) -> None:
    await store.upsert(OnboardedRepo(owner="o", name="a", state=OnboardingState.ONBOARDED))
    await store.upsert(OnboardedRepo(owner="o", name="b", state=OnboardingState.ONBOARDED))
    await store.archive("o", "b")
    visible = await store.list()
    assert {r.name for r in visible} == {"a"}
    everything = await store.list(include_archived=True)
    assert {r.name for r in everything} == {"a", "b"}


@pytest.mark.asyncio
async def test_archive_flips_state(store) -> None:
    await store.upsert(OnboardedRepo(owner="o", name="a", state=OnboardingState.ONBOARDED))
    row = await store.archive("o", "a")
    assert row is not None and row.state == OnboardingState.ARCHIVED


@pytest.mark.asyncio
async def test_archive_missing_returns_none(store) -> None:
    assert await store.archive("o", "ghost") is None
