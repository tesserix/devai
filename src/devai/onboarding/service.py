"""OnboardingService — the reusable orchestration layer.

Depends only on an injected SCM client (any ``SCMClient``) and an
``OnboardingStore``. No FastAPI, no globals — so it is trivially unit
tested with a fake SCM client + the in-memory store.

Responsibilities:
  * list_catalog  — org repos (GH App access list) + marker overlay, paged + cached
  * onboard       — open one PR per repo adding `.platform/devai.yaml`
  * merge         — merge an onboarding PR and flip the row to onboarded
  * assign_reviewer — request a GitHub reviewer on the onboarding PR
  * reconcile     — rebuild / refresh the cache from the marker files
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from devai.onboarding.marker import MARKER_PATH, synthesize_marker
from devai.onboarding.models import (
    OnboardedRepo,
    OnboardingMetadata,
    OnboardingState,
    RepoSnapshot,
)

if TYPE_CHECKING:
    from devai.onboarding.store import OnboardingStore

logger = logging.getLogger(__name__)

_CATALOG_TTL = 300.0      # 5 min — org repo list cache
_MARKER_TTL = 300.0       # 5 min — marker-presence probe cache
_ONBOARD_BRANCH = "devai/onboard-platform"
_MAX_CONCURRENCY = 8


@dataclass
class _CacheEntry:
    value: Any
    expires_at: float


class OnboardingService:
    def __init__(
        self,
        scm: Any,
        store: OnboardingStore,
        org: str = "tesserix",
        marker_path: str = MARKER_PATH,
    ) -> None:
        self._scm = scm
        self._store = store
        self._org = org
        self._marker = marker_path
        self._catalog_cache: dict[str, _CacheEntry] = {}
        self._marker_cache: dict[str, _CacheEntry] = {}

    # ----------------------------------------------------------------- #
    # Catalog
    # ----------------------------------------------------------------- #

    async def _installation_snapshots(self, refresh: bool) -> tuple[list[RepoSnapshot], bool]:
        """All repos the GH App / token can see, scoped to the org. Cached."""
        now = time.monotonic()
        entry = self._catalog_cache.get(self._org)
        if entry and not refresh and entry.expires_at > now:
            return entry.value, True

        try:
            raw = await self._scm.list_installation_repos(per_page=100)
        except NotImplementedError:
            raw = []
        snaps: list[RepoSnapshot] = []
        for r in raw:
            owner = (r.get("owner") or {}).get("login", "") or r.get("full_name", "/").split("/")[0]
            name = r.get("name", "")
            if not name:
                continue
            if owner and self._org and owner.lower() != self._org.lower():
                # token may span multiple orgs — scope to ours.
                continue
            snaps.append(
                RepoSnapshot(
                    owner=owner,
                    name=name,
                    default_branch=r.get("default_branch", "main") or "main",
                    description=r.get("description") or "",
                    language=r.get("language") or "",
                    private=bool(r.get("private")),
                    archived=bool(r.get("archived")),
                    html_url=r.get("html_url", ""),
                    pushed_at=r.get("pushed_at", ""),
                )
            )
        snaps.sort(key=lambda s: s.pushed_at, reverse=True)
        self._catalog_cache[self._org] = _CacheEntry(snaps, now + _CATALOG_TTL)
        return snaps, False

    async def _probe_markers(self, repos: list[RepoSnapshot], refresh: bool) -> dict[str, bool]:
        """Batch-probe marker presence, using the per-repo cache where warm."""
        now = time.monotonic()
        result: dict[str, bool] = {}
        to_probe: list[RepoSnapshot] = []
        for s in repos:
            entry = self._marker_cache.get(s.full_name)
            if entry and not refresh and entry.expires_at > now:
                result[s.full_name] = entry.value
            else:
                to_probe.append(s)
        if to_probe:
            probe_arg = [(s.owner, s.name, s.default_branch) for s in to_probe]
            try:
                fresh = await self._scm.probe_markers(probe_arg, self._marker)
            except NotImplementedError:
                fresh = {}
            for s in to_probe:
                present = bool(fresh.get(s.full_name, False))
                result[s.full_name] = present
                self._marker_cache[s.full_name] = _CacheEntry(present, now + _MARKER_TTL)
        return result

    async def list_catalog(
        self,
        query: str = "",
        page: int = 1,
        per_page: int = 30,
        refresh: bool = False,
        probe: bool = True,
    ) -> dict[str, Any]:
        snaps, cache_hit = await self._installation_snapshots(refresh)

        q = query.lower().strip()
        if q:
            snaps = [s for s in snaps if q in s.full_name.lower() or q in s.description.lower()]

        total = len(snaps)
        per_page = max(1, per_page)
        page = max(1, page)
        total_pages = max(1, (total + per_page - 1) // per_page)
        start = (page - 1) * per_page
        page_items = snaps[start : start + per_page]

        markers: dict[str, bool] = {}
        if probe and page_items:
            markers = await self._probe_markers(page_items, refresh)

        # Overlay persisted state (store wins over a bare marker probe).
        rows = {r.full_name: r for r in await self._store.list(include_archived=True)}
        for s in page_items:
            row = rows.get(s.full_name)
            s.has_marker = markers.get(s.full_name)
            if row is not None and row.state != OnboardingState.ARCHIVED:
                s.state = row.state
                s.pr_number = row.pr_number
                s.pr_url = row.pr_url
                s.draft = row.draft
            elif s.has_marker:
                s.state = OnboardingState.ONBOARDED
            else:
                s.state = OnboardingState.DISCOVERED

        onboarded_rows = await self._store.list(state=OnboardingState.ONBOARDED)
        return {
            "repos": [s.to_dict() for s in page_items],
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
            "has_prev": page > 1,
            "has_next": page < total_pages,
            "onboarded_count": len(onboarded_rows),
            "cache_hit": cache_hit,
            "org": self._org,
        }

    # ----------------------------------------------------------------- #
    # Onboard
    # ----------------------------------------------------------------- #

    async def onboard(
        self,
        repos: list[tuple[str, str]],
        onboarded_by: str = "operator",
        base_branch: str = "",
        description: str = "",
        dry_run: bool = False,
    ) -> dict[str, Any]:
        sem = asyncio.Semaphore(_MAX_CONCURRENCY)

        async def _one(owner: str, name: str) -> dict[str, Any]:
            async with sem:
                return await self._onboard_one(owner, name, onboarded_by, base_branch, description, dry_run)

        results = await asyncio.gather(
            *(_one(o, n) for o, n in repos), return_exceptions=True
        )

        succeeded: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        for (owner, name), res in zip(repos, results, strict=False):
            if isinstance(res, Exception):
                failed.append({"repo": f"{owner}/{name}", "error": str(res)})
            elif res.get("ok"):
                succeeded.append(res)
            else:
                failed.append({"repo": f"{owner}/{name}", "error": res.get("error", "unknown")})
        return {"succeeded": succeeded, "failed": failed, "dry_run": dry_run}

    async def _onboard_one(
        self,
        owner: str,
        name: str,
        onboarded_by: str,
        base_branch: str,
        description: str,
        dry_run: bool,
    ) -> dict[str, Any]:
        full = f"{owner}/{name}"
        existing = await self._store.get(owner, name)
        if existing and existing.state in (OnboardingState.ONBOARDED, OnboardingState.PENDING_PR):
            return {
                "ok": True,
                "repo": full,
                "status": "already",
                "state": str(existing.state),
                "pr_number": existing.pr_number,
                "pr_url": existing.pr_url,
            }

        default_branch = await self._scm.get_default_branch(full)

        # Already enrolled out-of-band? Adopt without a PR.
        if await self._marker_present(full, default_branch):
            row = await self._record_onboarded(owner, name, base_branch or default_branch, description, onboarded_by)
            self._marker_cache[full] = _CacheEntry(True, time.monotonic() + _MARKER_TTL)
            return {"ok": True, "repo": full, "status": "adopted", "state": str(row.state)}

        if dry_run:
            return {"ok": True, "repo": full, "status": "planned", "default_branch": default_branch}

        # Open the onboarding PR.
        try:
            await self._scm.create_branch(repo=full, branch_name=_ONBOARD_BRANCH, from_branch=default_branch)
        except Exception as e:  # noqa: BLE001 — 422 = branch exists, reuse it
            if "422" not in str(e):
                raise

        metadata = OnboardingMetadata(
            onboarded_at=datetime.now(UTC).isoformat(),
            onboarded_by=onboarded_by,
            default_base_branch=base_branch or default_branch,
            description=description,
        )
        await self._scm.create_or_update_file(
            repo=full,
            path=self._marker,
            content=synthesize_marker(metadata),
            message="chore(devai): onboard repo to the DevAI platform",
            branch=_ONBOARD_BRANCH,
        )
        pr = await self._scm.create_pull_request(
            repo=full,
            title="chore(devai): onboard to DevAI platform",
            body=(
                "Enrols this repo into the **DevAI** platform.\n\n"
                f"Adds `{self._marker}` — its presence on the default branch marks the\n"
                "repo as onboarded. After this merges the repo appears as **ONBOARDED**\n"
                "in the DevAI Repos page and is available for pipeline runs.\n\n"
                "Opened as a **draft** — review the marker, then **Mark ready** in the\n"
                "DevAI Repos page (or via GitHub) to promote it to a real PR.\n"
            ),
            head=_ONBOARD_BRANCH,
            base=default_branch,
            draft=True,
        )
        row = OnboardedRepo(
            owner=owner,
            name=name,
            state=OnboardingState.PENDING_PR,
            pr_number=pr.get("number"),
            pr_url=pr.get("html_url", ""),
            draft=True,
            default_base_branch=base_branch or default_branch,
            description=description,
            onboarded_by=onboarded_by,
        )
        await self._store.upsert(row)
        return {
            "ok": True,
            "repo": full,
            "status": "pr_open",
            "state": str(row.state),
            "pr_number": row.pr_number,
            "pr_url": row.pr_url,
            "draft": row.draft,
        }

    async def _marker_present(self, full: str, ref: str) -> bool:
        try:
            await self._scm.get_file_content(repo=full, path=self._marker, ref=ref)
            return True
        except Exception:  # noqa: BLE001
            return False

    async def _record_onboarded(
        self, owner: str, name: str, base_branch: str, description: str, onboarded_by: str
    ) -> OnboardedRepo:
        existing = await self._store.get(owner, name)
        row = OnboardedRepo(
            owner=owner,
            name=name,
            state=OnboardingState.ONBOARDED,
            default_base_branch=base_branch,
            description=description or (existing.description if existing else ""),
            onboarded_by=onboarded_by or (existing.onboarded_by if existing else ""),
            onboarded_at=datetime.now(UTC),
            pr_number=existing.pr_number if existing else None,
            pr_url=existing.pr_url if existing else "",
        )
        return await self._store.upsert(row)

    # ----------------------------------------------------------------- #
    # PR controls (merge + assign reviewer)
    # ----------------------------------------------------------------- #

    async def get(self, owner: str, name: str) -> OnboardedRepo | None:
        return await self._store.get(owner, name)

    async def list_onboarded(
        self, state: OnboardingState | str | None = None
    ) -> list[OnboardedRepo]:
        return await self._store.list(state=state)

    async def mark_ready(self, owner: str, name: str) -> OnboardedRepo:
        """Promote the draft onboarding PR to a real (ready-for-review) PR."""
        row = await self._store.get(owner, name)
        if row is None or not row.pr_number:
            raise ValueError(f"no open onboarding PR recorded for {owner}/{name}")
        await self._scm.mark_pull_request_ready(f"{owner}/{name}", row.pr_number)
        row.draft = False
        return await self._store.upsert(row)

    async def merge(self, owner: str, name: str, method: str = "squash") -> OnboardedRepo:
        row = await self._store.get(owner, name)
        if row is None or not row.pr_number:
            raise ValueError(f"no open onboarding PR recorded for {owner}/{name}")
        # A draft PR can't be merged on GitHub — promote it first so the
        # single "Merge" action works whether or not the user clicked Ready.
        if row.draft:
            try:
                await self._scm.mark_pull_request_ready(f"{owner}/{name}", row.pr_number)
                row.draft = False
            except Exception:  # noqa: BLE001 — fall through; merge will report the real error
                logger.warning("mark-ready before merge failed for %s/%s", owner, name)
        await self._scm.merge_pull_request(f"{owner}/{name}", row.pr_number, method=method)
        row.state = OnboardingState.ONBOARDED
        row.draft = False
        row.onboarded_at = datetime.now(UTC)
        self._marker_cache[row.full_name] = _CacheEntry(True, time.monotonic() + _MARKER_TTL)
        return await self._store.upsert(row)

    async def assign_reviewer(
        self, owner: str, name: str, reviewers: list[str]
    ) -> dict[str, Any]:
        row = await self._store.get(owner, name)
        if row is None or not row.pr_number:
            raise ValueError(f"no open onboarding PR recorded for {owner}/{name}")
        result = await self._scm.request_reviewers(f"{owner}/{name}", row.pr_number, reviewers)
        return {"repo": row.full_name, "pr_number": row.pr_number, "reviewers": reviewers, "result": result}

    async def archive(self, owner: str, name: str) -> OnboardedRepo | None:
        return await self._store.archive(owner, name)

    # ----------------------------------------------------------------- #
    # Reconcile — marker files are the source of truth
    # ----------------------------------------------------------------- #

    async def reconcile(self, org: str = "") -> dict[str, Any]:
        org = org or self._org
        report = _Report()
        rows = await self._store.list(include_archived=False)

        if rows:  # DB-first: refresh known rows, zero org-walk.
            for row in rows:
                report.scanned += 1
                try:
                    default_branch = await self._scm.get_default_branch(row.full_name)
                    present = await self._marker_present(row.full_name, default_branch)
                except Exception as e:  # noqa: BLE001
                    report.errors.append(f"{row.full_name}: {e}")
                    continue
                if present:
                    if row.state != OnboardingState.ONBOARDED:
                        row.state = OnboardingState.ONBOARDED
                        await self._store.upsert(row)
                    report.reconciled += 1
                elif row.state == OnboardingState.ONBOARDED:
                    row.state = OnboardingState.DORMANT
                    await self._store.upsert(row)
                    report.reconciled += 1
                else:
                    report.skipped += 1
            return report.to_dict()

        # Cold-start: walk the catalog with a marker probe and seed rows.
        snaps, _ = await self._installation_snapshots(refresh=True)
        markers = await self._probe_markers(snaps, refresh=True)
        for s in snaps:
            report.scanned += 1
            if markers.get(s.full_name):
                await self._store.upsert(
                    OnboardedRepo(
                        owner=s.owner,
                        name=s.name,
                        state=OnboardingState.ONBOARDED,
                        default_base_branch=s.default_branch,
                        description=s.description,
                        onboarded_at=datetime.now(UTC),
                    )
                )
                report.reconciled += 1
            else:
                report.skipped += 1
        return report.to_dict()


@dataclass
class _Report:
    scanned: int = 0
    reconciled: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "reconciled": self.reconciled,
            "skipped": self.skipped,
            "errors": self.errors,
        }
