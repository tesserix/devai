"""Onboarding FastAPI routes — wired against fakes on app.state."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from devai.onboarding.routes import router
from devai.onboarding.service import OnboardingService
from devai.onboarding.store import InMemoryOnboardingStore
from tests.unit._fake_scm import FakeSCM


def _repo(owner: str, name: str) -> dict:
    return {
        "full_name": f"{owner}/{name}",
        "name": name,
        "owner": {"login": owner},
        "description": "",
        "language": "Python",
        "private": True,
        "default_branch": "main",
        "html_url": f"https://github.com/{owner}/{name}",
        "pushed_at": "2026-05-01T00:00:00Z",
    }


def _client(scm: FakeSCM | None) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    if scm is not None:
        app.state.onboarding_service = OnboardingService(
            scm=scm, store=InMemoryOnboardingStore(), org="tesserix"
        )
    else:
        app.state.onboarding_service = None
    return TestClient(app)


def test_org_repos_503_when_service_missing() -> None:
    resp = _client(None).get("/api/scm/org/repos")
    assert resp.status_code == 503


def test_org_repos_lists_catalog() -> None:
    scm = FakeSCM(repos=[_repo("tesserix", "a"), _repo("tesserix", "b")], markers={"tesserix/a"})
    resp = _client(scm).get("/api/scm/org/repos?per_page=10")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    states = {r["name"]: r["state"] for r in body["repos"]}
    assert states["a"] == "onboarded"


def test_onboard_then_get_and_merge_flow() -> None:
    scm = FakeSCM(repos=[_repo("tesserix", "a")])
    client = _client(scm)

    onboard = client.post("/api/scm/onboarded", json={"repos": [{"owner": "tesserix", "name": "a"}]})
    assert onboard.status_code == 200
    assert onboard.json()["succeeded"][0]["status"] == "pr_open"

    got = client.get("/api/scm/onboarded/tesserix/a")
    assert got.status_code == 200
    assert got.json()["state"] == "pending_pr"

    merged = client.post("/api/scm/onboarded/tesserix/a/merge", json={"method": "squash"})
    assert merged.status_code == 200
    assert merged.json()["state"] == "onboarded"


def test_onboard_opens_draft_then_mark_ready_promotes() -> None:
    scm = FakeSCM(repos=[_repo("tesserix", "a")])
    client = _client(scm)

    onboard = client.post("/api/scm/onboarded", json={"repos": [{"owner": "tesserix", "name": "a"}]})
    assert onboard.status_code == 200
    assert onboard.json()["succeeded"][0]["draft"] is True

    ready = client.post("/api/scm/onboarded/tesserix/a/ready")
    assert ready.status_code == 200
    body = ready.json()
    assert body["draft"] is False
    assert body["state"] == "pending_pr"
    assert scm.count("mark_pull_request_ready") == 1


def test_mark_ready_without_pr_returns_409() -> None:
    scm = FakeSCM(repos=[_repo("tesserix", "a")])
    resp = _client(scm).post("/api/scm/onboarded/tesserix/a/ready")
    assert resp.status_code == 409


def test_get_unonboarded_returns_404() -> None:
    scm = FakeSCM(repos=[_repo("tesserix", "a")])
    resp = _client(scm).get("/api/scm/onboarded/tesserix/ghost")
    assert resp.status_code == 404


def test_assign_reviewer_requires_open_pr() -> None:
    scm = FakeSCM(repos=[_repo("tesserix", "a")])
    resp = _client(scm).post(
        "/api/scm/onboarded/tesserix/a/assign", json={"reviewers": ["mahesh"]}
    )
    assert resp.status_code == 409  # nothing onboarded yet


def test_reconcile_returns_report() -> None:
    scm = FakeSCM(repos=[_repo("tesserix", "a")], markers={"tesserix/a"})
    resp = _client(scm).post("/api/scm/onboarded/reconcile")
    assert resp.status_code == 200
    assert set(resp.json()) >= {"scanned", "reconciled", "skipped", "errors"}


# --------------------------------------------------------------------- #
# Input-validation hardening (security)
# --------------------------------------------------------------------- #


def test_onboard_rejects_path_traversal_owner() -> None:
    scm = FakeSCM(repos=[_repo("tesserix", "a")])
    resp = _client(scm).post(
        "/api/scm/onboarded", json={"repos": [{"owner": "..", "name": "etc"}]}
    )
    assert resp.status_code == 422
    # No SCM call was attempted for the malformed ref.
    assert scm.count("create_pull_request") == 0


def test_onboard_rejects_oversized_batch() -> None:
    scm = FakeSCM(repos=[_repo("tesserix", "a")])
    repos = [{"owner": "tesserix", "name": f"r{i}"} for i in range(101)]
    resp = _client(scm).post("/api/scm/onboarded", json={"repos": repos})
    assert resp.status_code == 422


def test_merge_rejects_bad_method() -> None:
    scm = FakeSCM(repos=[_repo("tesserix", "a")])
    resp = _client(scm).post(
        "/api/scm/onboarded/tesserix/a/merge", json={"method": "force; rm -rf"}
    )
    assert resp.status_code == 422


def test_assign_rejects_malformed_reviewer() -> None:
    scm = FakeSCM(repos=[_repo("tesserix", "a")])
    resp = _client(scm).post(
        "/api/scm/onboarded/tesserix/a/assign", json={"reviewers": ["evil reviewer!"]}
    )
    assert resp.status_code == 422
