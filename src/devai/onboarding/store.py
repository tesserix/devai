"""Persistence adapter for onboarding records.

Follows the DevAI adapter convention: one ABC declaring the minimum
surface, an in-memory implementation used for tests + graceful fallback,
and a Postgres implementation for production. The store is a *cache* —
the reconciler rebuilds it from `.platform/devai.yaml` files — so losing
it never loses onboarded state.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from datetime import UTC, datetime

from devai.onboarding.models import OnboardedRepo, OnboardingState

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class OnboardingStore(ABC):
    """Minimum surface every backend implements."""

    @abstractmethod
    async def upsert(self, repo: OnboardedRepo) -> OnboardedRepo:
        """Insert or update a record keyed by (owner, name)."""

    @abstractmethod
    async def get(self, owner: str, name: str) -> OnboardedRepo | None:
        """Fetch one record, or None when absent."""

    @abstractmethod
    async def list(
        self, state: OnboardingState | str | None = None, include_archived: bool = False
    ) -> list[OnboardedRepo]:
        """List records, optionally filtered by state."""

    @abstractmethod
    async def archive(self, owner: str, name: str) -> OnboardedRepo | None:
        """Soft-delete: flip a record to ARCHIVED. None when absent."""

    async def close(self) -> None:  # pragma: no cover - optional
        """Release backend resources. No-op by default."""
        return None


# --------------------------------------------------------------------------- #
# In-memory (tests + fallback)
# --------------------------------------------------------------------------- #


class InMemoryOnboardingStore(OnboardingStore):
    """Process-local store. Used in tests and when Postgres is unreachable."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], OnboardedRepo] = {}

    async def upsert(self, repo: OnboardedRepo) -> OnboardedRepo:
        key = (repo.owner, repo.name)
        now = _utcnow()
        existing = self._rows.get(key)
        repo.created_at = existing.created_at if existing else (repo.created_at or now)
        repo.updated_at = now
        self._rows[key] = repo
        return repo

    async def get(self, owner: str, name: str) -> OnboardedRepo | None:
        return self._rows.get((owner, name))

    async def list(
        self, state: OnboardingState | str | None = None, include_archived: bool = False
    ) -> list[OnboardedRepo]:
        want = str(state) if state is not None else None
        out: list[OnboardedRepo] = []
        for row in self._rows.values():
            if not include_archived and row.state == OnboardingState.ARCHIVED and want is None:
                continue
            if want is not None and str(row.state) != want:
                continue
            out.append(row)
        out.sort(key=lambda r: r.updated_at or _utcnow(), reverse=True)
        return out

    async def archive(self, owner: str, name: str) -> OnboardedRepo | None:
        row = self._rows.get((owner, name))
        if row is None:
            return None
        row.state = OnboardingState.ARCHIVED
        row.updated_at = _utcnow()
        return row


# --------------------------------------------------------------------------- #
# Postgres (asyncpg)
# --------------------------------------------------------------------------- #


class PostgresOnboardingStore(OnboardingStore):
    """asyncpg-backed store. Schema lives in tesserix-k8s (repo_onboarding)."""

    def __init__(self, pool: object) -> None:
        self._pool = pool

    @staticmethod
    def _row_to_model(row: object) -> OnboardedRepo:
        r = dict(row)  # type: ignore[arg-type]
        stack = r.get("detected_stack")
        if isinstance(stack, str):
            stack = json.loads(stack or "{}")
        tags = r.get("tags")
        if isinstance(tags, str):
            tags = json.loads(tags or "[]")
        return OnboardedRepo(
            owner=r["owner"],
            name=r["name"],
            state=OnboardingState(r["state"]),
            pr_number=r.get("pr_number"),
            pr_url=r.get("pr_url") or "",
            default_base_branch=r.get("default_base_branch") or "main",
            description=r.get("description") or "",
            detected_stack=stack or {},
            onboarded_at=r.get("onboarded_at"),
            onboarded_by=r.get("onboarded_by") or "",
            tags=tags or [],
            draft=bool(r.get("draft", False)),
            created_at=r.get("created_at"),
            updated_at=r.get("updated_at"),
        )

    async def upsert(self, repo: OnboardedRepo) -> OnboardedRepo:
        row = await self._pool.fetchrow(  # type: ignore[attr-defined]
            """
            INSERT INTO repo_onboarding
                (owner, name, state, pr_number, pr_url, default_base_branch,
                 description, detected_stack, onboarded_at, onboarded_by, tags, draft)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
            ON CONFLICT (owner, name) DO UPDATE SET
                state              = EXCLUDED.state,
                pr_number          = EXCLUDED.pr_number,
                pr_url             = EXCLUDED.pr_url,
                default_base_branch= EXCLUDED.default_base_branch,
                description        = EXCLUDED.description,
                detected_stack     = EXCLUDED.detected_stack,
                onboarded_at       = EXCLUDED.onboarded_at,
                onboarded_by       = EXCLUDED.onboarded_by,
                tags               = EXCLUDED.tags,
                draft              = EXCLUDED.draft,
                updated_at         = NOW()
            RETURNING *
            """,
            repo.owner,
            repo.name,
            str(repo.state),
            repo.pr_number,
            repo.pr_url,
            repo.default_base_branch,
            repo.description,
            json.dumps(repo.detected_stack or {}),
            repo.onboarded_at,
            repo.onboarded_by,
            json.dumps(repo.tags or []),
            repo.draft,
        )
        return self._row_to_model(row)

    async def get(self, owner: str, name: str) -> OnboardedRepo | None:
        row = await self._pool.fetchrow(  # type: ignore[attr-defined]
            "SELECT * FROM repo_onboarding WHERE owner = $1 AND name = $2", owner, name
        )
        return self._row_to_model(row) if row else None

    async def list(
        self, state: OnboardingState | str | None = None, include_archived: bool = False
    ) -> list[OnboardedRepo]:
        if state is not None:
            rows = await self._pool.fetch(  # type: ignore[attr-defined]
                "SELECT * FROM repo_onboarding WHERE state = $1 ORDER BY updated_at DESC",
                str(state),
            )
        elif include_archived:
            rows = await self._pool.fetch(  # type: ignore[attr-defined]
                "SELECT * FROM repo_onboarding ORDER BY updated_at DESC"
            )
        else:
            rows = await self._pool.fetch(  # type: ignore[attr-defined]
                "SELECT * FROM repo_onboarding WHERE state <> 'archived' ORDER BY updated_at DESC"
            )
        return [self._row_to_model(r) for r in rows]

    async def archive(self, owner: str, name: str) -> OnboardedRepo | None:
        row = await self._pool.fetchrow(  # type: ignore[attr-defined]
            """UPDATE repo_onboarding SET state = 'archived', updated_at = NOW()
               WHERE owner = $1 AND name = $2 RETURNING *""",
            owner,
            name,
        )
        return self._row_to_model(row) if row else None
