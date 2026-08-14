"""The ADK version catalogue an agent chooses from."""

from __future__ import annotations

import pytest

from devai.kit.versions import AdkVersionCatalogue, UnknownAdkVersion

RELEASES = [
    {"tag_name": "v0.1.1", "draft": False, "prerelease": False, "published_at": "2026-08-14T00:19:34Z"},
    {"tag_name": "v0.1.0", "draft": False, "prerelease": False, "published_at": "2026-08-13T23:45:24Z"},
    {"tag_name": "v0.0.9", "draft": False, "prerelease": False, "published_at": "2026-08-12T10:00:00Z"},
    {"tag_name": "v0.0.8", "draft": False, "prerelease": False, "published_at": "2026-08-11T10:00:00Z"},
    {"tag_name": "v0.0.7", "draft": False, "prerelease": False, "published_at": "2026-08-10T10:00:00Z"},
    {"tag_name": "v0.0.6", "draft": False, "prerelease": False, "published_at": "2026-08-09T10:00:00Z"},
]


def _fetcher(payload=RELEASES, *, calls=None):
    async def fetch() -> list[dict]:
        if calls is not None:
            calls.append(1)
        return payload

    return fetch


async def test_offers_the_latest_five_versions_newest_first():
    cat = AdkVersionCatalogue(fetch=_fetcher(), fallback="0.1.1")

    assert await cat.versions() == ["0.1.1", "0.1.0", "0.0.9", "0.0.8", "0.0.7"]


async def test_default_is_the_latest_version():
    cat = AdkVersionCatalogue(fetch=_fetcher(), fallback="0.0.1")

    assert await cat.default() == "0.1.1"


async def test_drafts_and_prereleases_are_not_offered():
    payload = [
        {"tag_name": "v0.2.0", "draft": True, "prerelease": False, "published_at": "2026-08-15T00:00:00Z"},
        {"tag_name": "v0.2.0rc1", "draft": False, "prerelease": True, "published_at": "2026-08-15T00:00:00Z"},
        *RELEASES[:2],
    ]
    cat = AdkVersionCatalogue(fetch=_fetcher(payload), fallback="0.1.1")

    assert await cat.versions() == ["0.1.1", "0.1.0"]


async def test_resolve_returns_the_default_when_nothing_was_asked_for():
    cat = AdkVersionCatalogue(fetch=_fetcher(), fallback="0.0.1")

    assert await cat.resolve(None) == "0.1.1"
    assert await cat.resolve("") == "0.1.1"


async def test_resolve_accepts_an_offered_version_with_or_without_its_v_prefix():
    cat = AdkVersionCatalogue(fetch=_fetcher(), fallback="0.1.1")

    assert await cat.resolve("0.1.0") == "0.1.0"
    assert await cat.resolve("v0.1.0") == "0.1.0"


async def test_resolve_refuses_a_version_that_is_not_offered():
    cat = AdkVersionCatalogue(fetch=_fetcher(), fallback="0.1.1")

    # Off the list entirely, and on the list but too old to still be offered.
    with pytest.raises(UnknownAdkVersion):
        await cat.resolve("9.9.9")
    with pytest.raises(UnknownAdkVersion):
        await cat.resolve("0.0.6")


async def test_a_github_outage_falls_back_to_the_version_in_the_image():
    async def boom() -> list[dict]:
        raise RuntimeError("github is down")

    cat = AdkVersionCatalogue(fetch=boom, fallback="0.1.1")

    assert await cat.versions() == ["0.1.1"]
    assert await cat.resolve("0.1.1") == "0.1.1"


async def test_the_list_is_cached_rather_than_fetched_per_call():
    calls: list[int] = []
    cat = AdkVersionCatalogue(fetch=_fetcher(calls=calls), fallback="0.1.1", ttl_seconds=300)

    await cat.versions()
    await cat.versions()
    await cat.default()

    assert len(calls) == 1


async def test_the_cache_expires_so_a_new_release_becomes_available():
    clock = {"t": 0.0}
    calls: list[int] = []
    cat = AdkVersionCatalogue(fetch=_fetcher(calls=calls), fallback="0.1.1", ttl_seconds=300, now=lambda: clock["t"])

    await cat.versions()
    clock["t"] = 400.0
    await cat.versions()

    assert len(calls) == 2
