"""Which release of `tesserix-adk` a run is built on.

The kit is 0.1.x and its own changelog records breaking changes between minor
releases, so the version is one more thing that changes a result — it belongs
next to the model and the prompt in what a sandbox pins, not in the image alone.

An agent chooses from the five most recent releases and gets the newest by
default. Older releases stay reachable so a run can be reproduced against the
kit it originally ran on; anything further back is not offered, because a
version nobody is prepared to support is not a choice.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as installed_version
from typing import Any

import httpx

logger = logging.getLogger(__name__)

ADK_REPO = "tesserix/agent-development-kit"
ADK_PACKAGE = "tesserix-adk"
OFFERED = 5
_DEFAULT_TTL_SECONDS = 15 * 60

Fetch = Callable[[], Awaitable[list[dict]]]


class UnknownAdkVersion(ValueError):
    """A version that is not among the ones currently offered."""


def normalize(version: str) -> str:
    return (version or "").strip().removeprefix("v")


class AdkVersionCatalogue:
    """The offered `tesserix-adk` releases, newest first.

    ``fallback`` is the version built into the image. It is what the catalogue
    answers with when the release list cannot be read, so a GitHub outage
    degrades the *choice* rather than the ability to run at all.
    """

    def __init__(
        self,
        *,
        fetch: Fetch,
        fallback: str,
        ttl_seconds: int = _DEFAULT_TTL_SECONDS,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetch = fetch
        self._fallback = normalize(fallback)
        self._ttl = ttl_seconds
        self._now = now
        self._cached: list[str] | None = None
        self._cached_at = 0.0

    async def versions(self) -> list[str]:
        if self._cached is not None and self._now() - self._cached_at < self._ttl:
            return list(self._cached)
        self._cached = await self._load()
        self._cached_at = self._now()
        return list(self._cached)

    async def default(self) -> str:
        return (await self.versions())[0]

    async def resolve(self, requested: str | None) -> str:
        """Pin a choice. Empty means "the default"; anything unoffered is refused."""
        offered = await self.versions()
        if not requested:
            return offered[0]
        wanted = normalize(requested)
        if wanted not in offered:
            raise UnknownAdkVersion(f"tesserix-adk {wanted} is not offered; choose one of {', '.join(offered)}")
        return wanted

    async def _load(self) -> list[str]:
        try:
            releases = await self._fetch()
        except Exception:  # noqa: BLE001 — the release list is a convenience, not a dependency
            logger.warning("adk: release list unavailable — offering the built-in %s only", self._fallback)
            return [self._fallback]
        published = [
            normalize(str(r.get("tag_name", "")))
            for r in releases
            if not r.get("draft") and not r.get("prerelease") and r.get("tag_name")
        ]
        return published[:OFFERED] or [self._fallback]


def bundled_version() -> str:
    """The kit release built into this image — the floor the catalogue falls back to."""
    try:
        return installed_version(ADK_PACKAGE)
    except PackageNotFoundError:
        return "0.0.0"


def github_releases(repo: str, *, token: str) -> Fetch:
    """Read a repo's releases. The kit repo is private, so a token is usually needed."""
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    async def fetch() -> list[dict]:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"https://api.github.com/repos/{repo}/releases", headers=headers)
            response.raise_for_status()
            payload = response.json()
        return payload if isinstance(payload, list) else []

    return fetch


def create_adk_catalogue(settings: Any) -> AdkVersionCatalogue:
    return AdkVersionCatalogue(
        fetch=github_releases(ADK_REPO, token=str(getattr(settings, "scm_token", "") or "")),
        fallback=bundled_version(),
    )


__all__ = [
    "ADK_PACKAGE",
    "ADK_REPO",
    "OFFERED",
    "AdkVersionCatalogue",
    "UnknownAdkVersion",
    "bundled_version",
    "create_adk_catalogue",
    "github_releases",
    "normalize",
]
