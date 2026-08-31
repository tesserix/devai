"""A user with their own Source Control connection sees THEIR repos, not the org's."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import devai.onboarding.routes as onboarding_routes
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


class _FakeResolver:
    """resolve_with_overlay → (client, overlay) or None, like PrincipalSCMResolver."""

    def __init__(self, client: Any = None, org: str = "") -> None:
        self._client = client
        self._org = org

    async def resolve_with_overlay(self, principal: Any) -> tuple[Any, Any] | None:
        if self._client is None:
            return None
        return self._client, SimpleNamespace(scm_organization=self._org)


@pytest.fixture(autouse=True)
def _fresh_scoped_cache():
    onboarding_routes._scoped.clear()
    yield
    onboarding_routes._scoped.clear()


def _client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    platform_scm: FakeSCM,
    resolver: _FakeResolver | None = None,
    email: str = "dev@example.com",
    require_auth: bool = False,
    roles: list[str] | None = None,
) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.state.onboarding_service = OnboardingService(scm=platform_scm, store=InMemoryOnboardingStore(), org="tesserix")
    app.state.scm_resolver = resolver
    app.state.config = SimpleNamespace(require_auth=require_auth)

    async def _principal(request: Any) -> Any:
        if not email:
            return None
        return SimpleNamespace(email=email, uid="", display_name="", roles=list(roles or []))

    monkeypatch.setattr(onboarding_routes, "extract_principal", _principal)
    return TestClient(app)


def test_a_user_with_their_own_github_sees_their_org(monkeypatch: pytest.MonkeyPatch) -> None:
    platform = FakeSCM(repos=[_repo("tesserix", "devai")])
    theirs = FakeSCM(repos=[_repo("acme", "shop"), _repo("acme", "api")])
    client = _client(monkeypatch, platform_scm=platform, resolver=_FakeResolver(theirs, org="acme"))

    body = client.get("/api/scm/org/repos").json()

    assert body["org"] == "acme"
    assert sorted(r["name"] for r in body["repos"]) == ["api", "shop"]


def test_a_user_without_a_connection_gets_the_platform_view(monkeypatch: pytest.MonkeyPatch) -> None:
    platform = FakeSCM(repos=[_repo("tesserix", "devai")])
    client = _client(monkeypatch, platform_scm=platform, resolver=_FakeResolver(None))

    body = client.get("/api/scm/org/repos").json()

    assert body["org"] == "tesserix"
    assert [r["name"] for r in body["repos"]] == ["devai"]


def test_no_org_preference_lists_everything_their_token_sees(monkeypatch: pytest.MonkeyPatch) -> None:
    theirs = FakeSCM(repos=[_repo("me", "blog"), _repo("some-org", "lib")])
    client = _client(monkeypatch, platform_scm=FakeSCM(repos=[]), resolver=_FakeResolver(theirs, org=""))

    body = client.get("/api/scm/org/repos").json()

    assert sorted(r["full_name"] for r in body["repos"]) == ["me/blog", "some-org/lib"]


def test_onboarded_listing_is_scoped_to_the_viewer(monkeypatch: pytest.MonkeyPatch) -> None:
    platform = FakeSCM(repos=[_repo("tesserix", "devai")])
    theirs = FakeSCM(repos=[_repo("acme", "shop")])
    client = _client(monkeypatch, platform_scm=platform, resolver=_FakeResolver(theirs, org="acme"))

    onboard = client.post("/api/scm/onboarded", json={"repos": [{"owner": "acme", "name": "shop"}]})
    assert onboard.status_code == 200

    # Their listing shows their row; the platform ledger row from another
    # tenant's org must not appear.
    rows = client.get("/api/scm/onboarded").json()
    assert [r["owner"] for r in rows] == ["acme"]


def test_enforced_auth_hides_the_platform_org_from_non_admins(monkeypatch: pytest.MonkeyPatch) -> None:
    platform = FakeSCM(repos=[_repo("tesserix", "devai")])
    client = _client(monkeypatch, platform_scm=platform, resolver=_FakeResolver(None), require_auth=True)

    resp = client.get("/api/scm/org/repos")

    assert resp.status_code == 403
    assert "Settings" in resp.json()["detail"]


def test_enforced_auth_still_gives_admins_the_platform_org(monkeypatch: pytest.MonkeyPatch) -> None:
    platform = FakeSCM(repos=[_repo("tesserix", "devai")])
    client = _client(
        monkeypatch,
        platform_scm=platform,
        resolver=_FakeResolver(None),
        require_auth=True,
        roles=["admin"],
    )

    body = client.get("/api/scm/org/repos").json()

    assert body["org"] == "tesserix"


def test_enforced_auth_lets_a_connected_non_admin_use_their_own(monkeypatch: pytest.MonkeyPatch) -> None:
    theirs = FakeSCM(repos=[_repo("acme", "shop")])
    client = _client(
        monkeypatch,
        platform_scm=FakeSCM(repos=[_repo("tesserix", "devai")]),
        resolver=_FakeResolver(theirs, org="acme"),
        require_auth=True,
    )

    body = client.get("/api/scm/org/repos").json()

    assert body["org"] == "acme"
    assert [r["full_name"] for r in body["repos"]] == ["acme/shop"]


def test_creating_a_repo_without_an_org_is_a_clear_409(monkeypatch: pytest.MonkeyPatch) -> None:
    theirs = FakeSCM(repos=[_repo("me", "blog")])
    client = _client(monkeypatch, platform_scm=FakeSCM(repos=[]), resolver=_FakeResolver(theirs, org=""))

    resp = client.post("/api/scm/onboarded/create", json={"name": "new-repo"})

    assert resp.status_code == 409
    assert "Organization" in resp.json()["detail"]
