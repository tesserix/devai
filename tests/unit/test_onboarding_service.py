"""OnboardingService behaviour — fake SCM + in-memory store."""

from __future__ import annotations

import pytest

from devai.onboarding.models import OnboardingState
from devai.onboarding.service import OnboardingService
from devai.onboarding.store import InMemoryOnboardingStore
from tests.unit._fake_scm import FakeSCM


def _repo(owner: str, name: str, **extra) -> dict:
    return {
        "full_name": f"{owner}/{name}",
        "name": name,
        "owner": {"login": owner},
        "description": extra.get("description", ""),
        "language": extra.get("language", ""),
        "private": extra.get("private", False),
        "default_branch": "main",
        "html_url": f"https://github.com/{owner}/{name}",
        "pushed_at": extra.get("pushed_at", "2026-05-01T00:00:00Z"),
    }


def _service(scm: FakeSCM) -> OnboardingService:
    return OnboardingService(scm=scm, store=InMemoryOnboardingStore(), org="tesserix")


# --------------------------------------------------------------------- #
# Catalog
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_catalog_scopes_to_org_and_paginates() -> None:
    repos = [_repo("tesserix", f"repo{i}", pushed_at=f"2026-05-{i:02d}T00:00:00Z") for i in range(1, 8)]
    repos.append(_repo("someone-else", "external"))  # different org — must be excluded
    scm = FakeSCM(repos=repos)
    svc = _service(scm)

    page1 = await svc.list_catalog(page=1, per_page=3, probe=False)
    assert page1["total"] == 7  # external repo filtered out
    assert page1["total_pages"] == 3
    assert len(page1["repos"]) == 3
    assert page1["has_next"] is True and page1["has_prev"] is False
    # newest pushed first
    assert page1["repos"][0]["name"] == "repo7"


@pytest.mark.asyncio
async def test_catalog_overlays_marker_as_onboarded() -> None:
    scm = FakeSCM(repos=[_repo("tesserix", "a"), _repo("tesserix", "b")], markers={"tesserix/a"})
    svc = _service(scm)
    page = await svc.list_catalog(per_page=10, probe=True)
    by_name = {r["name"]: r for r in page["repos"]}
    assert by_name["a"]["state"] == "onboarded"
    assert by_name["a"]["onboarded"] is True
    assert by_name["b"]["state"] == "discovered"


@pytest.mark.asyncio
async def test_catalog_second_call_is_cache_hit() -> None:
    scm = FakeSCM(repos=[_repo("tesserix", "a")])
    svc = _service(scm)
    first = await svc.list_catalog(probe=False)
    second = await svc.list_catalog(probe=False)
    assert first["cache_hit"] is False
    assert second["cache_hit"] is True
    assert scm.count("list_installation_repos") == 1


# --------------------------------------------------------------------- #
# Onboard
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_onboard_opens_pr_and_records_pending() -> None:
    scm = FakeSCM(repos=[_repo("tesserix", "a")])
    svc = _service(scm)
    result = await svc.onboard([("tesserix", "a")], onboarded_by="samyak")
    assert len(result["succeeded"]) == 1
    s = result["succeeded"][0]
    assert s["status"] == "pr_open"
    assert s["pr_number"]
    # marker written to the onboarding branch + PR opened against default
    assert scm.count("create_or_update_file") == 1
    assert scm.count("create_pull_request") == 1
    row = await svc.get("tesserix", "a")
    assert row is not None and row.state == OnboardingState.PENDING_PR


@pytest.mark.asyncio
async def test_onboard_opens_a_draft_pr() -> None:
    scm = FakeSCM(repos=[_repo("tesserix", "a")])
    svc = _service(scm)
    result = await svc.onboard([("tesserix", "a")], onboarded_by="samyak")
    assert result["succeeded"][0]["draft"] is True
    # The PR was opened with draft=True on the wire.
    pr_call = next(c for c in scm.calls if c[0] == "create_pull_request")
    assert pr_call[2]["draft"] is True
    row = await svc.get("tesserix", "a")
    assert row is not None and row.draft is True


@pytest.mark.asyncio
async def test_mark_ready_promotes_draft_to_real_pr() -> None:
    scm = FakeSCM(repos=[_repo("tesserix", "a")])
    svc = _service(scm)
    await svc.onboard([("tesserix", "a")])
    row = await svc.mark_ready("tesserix", "a")
    assert row.draft is False
    assert scm.count("mark_pull_request_ready") == 1
    # Still pending until merged — mark-ready doesn't change lifecycle state.
    assert row.state == OnboardingState.PENDING_PR


@pytest.mark.asyncio
async def test_mark_ready_without_pr_raises() -> None:
    scm = FakeSCM(repos=[_repo("tesserix", "a")])
    svc = _service(scm)
    with pytest.raises(ValueError):
        await svc.mark_ready("tesserix", "a")


@pytest.mark.asyncio
async def test_merge_promotes_draft_then_merges() -> None:
    scm = FakeSCM(repos=[_repo("tesserix", "a")])
    svc = _service(scm)
    await svc.onboard([("tesserix", "a")])
    row = await svc.merge("tesserix", "a")
    # Draft PR can't merge directly, so merge() promotes it first.
    assert scm.count("mark_pull_request_ready") == 1
    assert scm.count("merge_pull_request") == 1
    assert row.state == OnboardingState.ONBOARDED
    assert row.draft is False


@pytest.mark.asyncio
async def test_onboard_is_idempotent_when_already_pending() -> None:
    scm = FakeSCM(repos=[_repo("tesserix", "a")])
    svc = _service(scm)
    await svc.onboard([("tesserix", "a")])
    again = await svc.onboard([("tesserix", "a")])
    assert again["succeeded"][0]["status"] == "already"
    # No second PR opened.
    assert scm.count("create_pull_request") == 1


@pytest.mark.asyncio
async def test_onboard_adopts_repo_that_already_has_marker() -> None:
    scm = FakeSCM(repos=[_repo("tesserix", "a")], markers={"tesserix/a"})
    svc = _service(scm)
    result = await svc.onboard([("tesserix", "a")])
    assert result["succeeded"][0]["status"] == "adopted"
    assert scm.count("create_pull_request") == 0
    row = await svc.get("tesserix", "a")
    assert row is not None and row.state == OnboardingState.ONBOARDED


@pytest.mark.asyncio
async def test_onboard_dry_run_has_no_side_effects() -> None:
    scm = FakeSCM(repos=[_repo("tesserix", "a")])
    svc = _service(scm)
    result = await svc.onboard([("tesserix", "a")], dry_run=True)
    assert result["succeeded"][0]["status"] == "planned"
    assert scm.count("create_pull_request") == 0
    assert await svc.get("tesserix", "a") is None


@pytest.mark.asyncio
async def test_onboard_bulk_isolates_per_repo_failure() -> None:
    class FlakySCM(FakeSCM):
        async def get_default_branch(self, repo: str) -> str:
            if repo == "tesserix/bad":
                raise RuntimeError("boom")
            return "main"

    scm = FlakySCM(repos=[_repo("tesserix", "good"), _repo("tesserix", "bad")])
    svc = _service(scm)
    result = await svc.onboard([("tesserix", "good"), ("tesserix", "bad")])
    assert {s["repo"] for s in result["succeeded"]} == {"tesserix/good"}
    assert {f["repo"] for f in result["failed"]} == {"tesserix/bad"}


# --------------------------------------------------------------------- #
# Merge + assign reviewer
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_merge_flips_pending_to_onboarded() -> None:
    scm = FakeSCM(repos=[_repo("tesserix", "a")])
    svc = _service(scm)
    await svc.onboard([("tesserix", "a")])
    row = await svc.merge("tesserix", "a")
    assert row.state == OnboardingState.ONBOARDED
    assert row.onboarded_at is not None
    assert scm.count("merge_pull_request") == 1


@pytest.mark.asyncio
async def test_merge_without_pr_raises() -> None:
    svc = _service(FakeSCM())
    with pytest.raises(ValueError):
        await svc.merge("tesserix", "never-onboarded")


@pytest.mark.asyncio
async def test_assign_reviewer_calls_scm() -> None:
    scm = FakeSCM(repos=[_repo("tesserix", "a")])
    svc = _service(scm)
    await svc.onboard([("tesserix", "a")])
    out = await svc.assign_reviewer("tesserix", "a", ["mahesh"])
    assert out["reviewers"] == ["mahesh"]
    assert scm.count("request_reviewers") == 1


# --------------------------------------------------------------------- #
# Reconcile
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_reconcile_db_first_marks_dormant_when_marker_gone() -> None:
    scm = FakeSCM(repos=[_repo("tesserix", "a")], markers={"tesserix/a"})
    svc = _service(scm)
    await svc.onboard([("tesserix", "a")])  # adopted → onboarded (has marker)
    # Marker deleted out-of-band:
    scm.markers.discard("tesserix/a")
    report = await svc.reconcile()
    assert report["scanned"] == 1
    row = await svc.get("tesserix", "a")
    assert row is not None and row.state == OnboardingState.DORMANT


@pytest.mark.asyncio
async def test_reconcile_cold_start_seeds_from_markers() -> None:
    scm = FakeSCM(
        repos=[_repo("tesserix", "a"), _repo("tesserix", "b")],
        markers={"tesserix/b"},
    )
    svc = _service(scm)  # empty store → cold start
    report = await svc.reconcile()
    assert report["reconciled"] == 1
    b = await svc.get("tesserix", "b")
    assert b is not None and b.state == OnboardingState.ONBOARDED
    assert await svc.get("tesserix", "a") is None


@pytest.mark.asyncio
async def test_reconcile_db_first_uses_one_batched_probe() -> None:
    # The DB-first path must fold the whole org into a single batched GraphQL
    # probe — no per-repo get_default_branch / get_file_content round trips.
    scm = FakeSCM(
        repos=[_repo("tesserix", "a"), _repo("tesserix", "b")],
        markers={"tesserix/a", "tesserix/b"},
    )
    svc = _service(scm)
    await svc.onboard([("tesserix", "a"), ("tesserix", "b")])  # adopted → onboarded
    scm.calls.clear()
    await svc.reconcile()
    assert scm.count("probe_markers") == 1
    assert scm.count("get_default_branch") == 0


@pytest.mark.asyncio
async def test_reconcile_does_not_false_offload_on_unknown_probe() -> None:
    # When the probe omits a repo (e.g. a GraphQL batch errored), an onboarded
    # repo must stay ONBOARDED — an unknown verdict is never "marker gone".
    class FlakyProbeSCM(FakeSCM):
        async def probe_markers(self, repos, marker_path):
            self._record("probe_markers", repos, marker_path)
            return {}  # nothing came back this round

    scm = FlakyProbeSCM(repos=[_repo("tesserix", "a")], markers={"tesserix/a"})
    svc = _service(scm)
    await svc.onboard([("tesserix", "a")])  # onboarded
    report = await svc.reconcile()
    assert report["scanned"] == 0  # nothing probed → nothing touched
    row = await svc.get("tesserix", "a")
    assert row is not None and row.state == OnboardingState.ONBOARDED


# --------------------------------------------------------------------- #
# Catalog self-healing cache
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_catalog_persists_discovered_marker_to_db() -> None:
    # Viewing the catalog adopts a repo that already carries the marker, so
    # the DB cache stays true to the marker files without a manual reconcile.
    scm = FakeSCM(repos=[_repo("tesserix", "a")], markers={"tesserix/a"})
    svc = _service(scm)
    assert await svc.get("tesserix", "a") is None
    await svc.list_catalog(per_page=10, probe=True)
    row = await svc.get("tesserix", "a")
    assert row is not None and row.state == OnboardingState.ONBOARDED


@pytest.mark.asyncio
async def test_catalog_offloads_removed_marker_to_dormant() -> None:
    scm = FakeSCM(repos=[_repo("tesserix", "a")], markers={"tesserix/a"})
    svc = _service(scm)
    await svc.onboard([("tesserix", "a")])  # adopted → onboarded
    scm.markers.discard("tesserix/a")  # deleted out-of-band
    page = await svc.list_catalog(per_page=10, probe=True, refresh=True)
    by_name = {r["name"]: r for r in page["repos"]}
    assert by_name["a"]["state"] == "dormant"
    row = await svc.get("tesserix", "a")
    assert row is not None and row.state == OnboardingState.DORMANT


@pytest.mark.asyncio
async def test_catalog_keeps_onboarded_repo_onboarded() -> None:
    # An already-onboarded repo always renders ONBOARDED and is not rewritten.
    scm = FakeSCM(repos=[_repo("tesserix", "a")], markers={"tesserix/a"})
    svc = _service(scm)
    await svc.onboard([("tesserix", "a")])
    page = await svc.list_catalog(per_page=10, probe=True, refresh=True)
    assert {r["name"]: r["state"] for r in page["repos"]}["a"] == "onboarded"
