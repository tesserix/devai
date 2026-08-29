"""Team scoping on the run REPO routes.

``/api/runs/{run_id}/repo/tree|file|events`` serve a run's repository content.
They resolved the run by id alone, so a member of one team could read another
team's source through a run id.
"""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from devai.identity import Principal
from devai.webhook.repo_routes import router

RUNS = {
    "run-alpha": {"run_id": "run-alpha", "team_id": "team-a", "context": {"repo_full_name": "o/a"}},
    "run-beta": {"run_id": "run-beta", "team_id": "team-b", "context": {"repo_full_name": "o/b"}},
}


class _FakeRedis:
    async def get(self, key):
        return None


class _FakeState:
    def __init__(self):
        self.redis = _FakeRedis()

    async def get_run(self, run_id):
        return RUNS.get(run_id)


class _FakeTeams:
    def can_dispatch(self, principal, team_id):
        if not team_id or principal is None or not principal.team_ids:
            return True
        return team_id in principal.team_ids


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    app.include_router(router)
    app.state.config = SimpleNamespace(require_auth=True)
    app.state.state_manager = _FakeState()
    app.state.team_service = _FakeTeams()

    async def fake_extract(request):
        email = request.headers.get("x-test-user", "")
        if not email:
            return None
        teams = [t for t in request.headers.get("x-test-teams", "").split(",") if t]
        return Principal(email=email, uid=email, auth_provider="test", team_ids=teams)

    monkeypatch.setattr("devai.authz.extract_principal", fake_extract)
    return TestClient(app)


def _as(email, teams=""):
    return {"x-test-user": email, "x-test-teams": teams}


def test_non_member_gets_404_on_other_team_repo_tree(client):
    r = client.get("/api/runs/run-beta/repo/tree", headers=_as("a@x.io", "team-a"))
    assert r.status_code == 404


def test_non_member_gets_404_on_other_team_repo_file(client):
    r = client.get(
        "/api/runs/run-beta/repo/file",
        params={"path": "README.md"},
        headers=_as("a@x.io", "team-a"),
    )
    assert r.status_code == 404


def test_non_member_gets_404_on_other_team_repo_events(client):
    r = client.get("/api/runs/run-beta/repo/events", headers=_as("a@x.io", "team-a"))
    assert r.status_code == 404


def test_anonymous_is_rejected_when_require_auth_on(client):
    r = client.get("/api/runs/run-alpha/repo/tree")
    assert r.status_code == 401


def test_unknown_run_is_404(client):
    r = client.get("/api/runs/nope/repo/tree", headers=_as("a@x.io", "team-a"))
    assert r.status_code == 404
