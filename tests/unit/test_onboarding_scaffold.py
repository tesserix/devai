"""Create-from-scratch onboarding: the default scaffold + create_and_onboard."""

from __future__ import annotations

import httpx
import pytest
import yaml

from devai.onboarding.marker import MARKER_PATH
from devai.onboarding.models import OnboardingState
from devai.onboarding.scaffold import default_scaffold_files
from devai.onboarding.service import OnboardingService
from devai.onboarding.store import InMemoryOnboardingStore
from tests.unit._fake_scm import FakeSCM


def test_scaffold_has_expected_files_and_quality_gates() -> None:
    files = default_scaffold_files("tesserix/widget", description="A widget service")
    paths = {f.path for f in files}
    assert {
        "README.md",
        ".gitignore",
        ".editorconfig",
        ".github/pull_request_template.md",
        ".github/CODEOWNERS",
        ".github/dependabot.yml",
        ".github/workflows/ci.yml",
        ".github/workflows/pr.yml",
        ".github/workflows/release.yml",
    } <= paths
    # The marker is NOT part of the scaffold set — the service adds it.
    assert MARKER_PATH not in paths


def test_scaffold_yaml_files_parse() -> None:
    by_path = {f.path: f.content for f in default_scaffold_files("tesserix/widget")}
    for p in (
        ".github/workflows/ci.yml",
        ".github/workflows/pr.yml",
        ".github/workflows/release.yml",
        ".github/dependabot.yml",
    ):
        doc = yaml.safe_load(by_path[p])
        assert isinstance(doc, dict), f"{p} should parse to a mapping"
    # CI gates the three stacks.
    ci = by_path[".github/workflows/ci.yml"]
    assert "hashFiles('package.json')" in ci
    assert "hashFiles('pyproject.toml', 'requirements.txt')" in ci
    assert "hashFiles('go.mod')" in ci


def test_scaffold_has_no_ai_tooling_references() -> None:
    # Project rule: never write CLAUDE.md / AI-tool references into a repo.
    blob = "\n".join(f.path + "\n" + f.content for f in default_scaffold_files("tesserix/widget"))
    low = blob.lower()
    for banned in ("claude", "anthropic", "copilot", "co-authored-by"):
        assert banned not in low, f"scaffold must not mention {banned!r}"


@pytest.mark.asyncio
async def test_create_and_onboard_seeds_marker_and_records_onboarded() -> None:
    scm = FakeSCM(default_branch="main")
    store = InMemoryOnboardingStore()
    svc = OnboardingService(scm=scm, store=store, org="tesserix")

    result = await svc.create_and_onboard("widget", description="A widget service", onboarded_by="alice@example.com")

    assert result["ok"] is True
    assert result["repo"] == "tesserix/widget"
    assert result["state"] == "onboarded"
    assert result["branch_protected"] is True
    assert MARKER_PATH in result["files_created"]
    assert "README.md" in result["files_created"]

    # Repo created empty so the scaffold owns the README.
    create = next(c for c in scm.calls if c[0] == "create_repo")
    assert create[2]["auto_init"] is False

    # The marker was committed with valid onboarding metadata.
    marker_writes = [c for c in scm.calls if c[0] == "create_or_update_file" and c[2].get("path") == MARKER_PATH]
    assert len(marker_writes) == 1

    # Branch protection was applied to the default branch.
    assert any(c[0] == "set_branch_protection" and c[2]["branch"] == "main" for c in scm.calls)

    # The store now has an ONBOARDED row.
    row = await store.get("tesserix", "widget")
    assert row is not None and row.state == OnboardingState.ONBOARDED
    assert row.onboarded_by == "alice@example.com"


@pytest.mark.asyncio
async def test_create_and_onboard_idempotent_when_already_onboarded() -> None:
    # A retry of an already-onboarded repo returns success (not 409/ValueError)
    # and makes no GitHub calls — the slow create can be retried by the edge.
    from devai.onboarding.models import OnboardedRepo

    scm = FakeSCM()
    store = InMemoryOnboardingStore()
    await store.upsert(OnboardedRepo(owner="tesserix", name="widget", state=OnboardingState.ONBOARDED))
    svc = OnboardingService(scm=scm, store=store, org="tesserix")

    result = await svc.create_and_onboard("widget")
    assert result["ok"] is True
    assert result["state"] == "onboarded"
    assert result["already_onboarded"] is True
    # No GitHub calls — pure fast-path.
    assert not any(c[0] == "create_repo" for c in scm.calls)


class _ExistingRepoSCM(FakeSCM):
    """create_repo raises 'already exists' (409); the flow must adopt + finish."""

    async def create_repo(self, *args: object, **kwargs: object) -> dict:
        self._record("create_repo", attempted=True)
        req = httpx.Request("POST", "https://api.github.com/orgs/tesserix/repos")
        raise httpx.HTTPStatusError("exists", request=req, response=httpx.Response(422, request=req))

    async def get_repo_info(self, repo: str) -> dict:
        return {"full_name": repo, "default_branch": "main", "html_url": f"https://github.com/{repo}"}


@pytest.mark.asyncio
async def test_create_and_onboard_adopts_existing_repo_on_retry() -> None:
    # When the repo already exists (retry / out-of-band create), adopt it:
    # finish scaffolding + marker and record ONBOARDED instead of 409.
    scm = _ExistingRepoSCM(default_branch="main")
    store = InMemoryOnboardingStore()
    svc = OnboardingService(scm=scm, store=store, org="tesserix")

    result = await svc.create_and_onboard("widget", onboarded_by="alice@example.com")
    assert result["ok"] is True
    assert result["adopted"] is True
    assert result["state"] == "onboarded"
    assert MARKER_PATH in result["files_created"]
    row = await store.get("tesserix", "widget")
    assert row is not None and row.state == OnboardingState.ONBOARDED
