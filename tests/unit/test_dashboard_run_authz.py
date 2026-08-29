"""Team scoping on the legacy dashboard run READ routes.

Run control (pause/resume/stop) already enforces membership. The read routes
returned the same run — requirements, A2A messages, agent outputs — to any
caller, so a member of one team could read another team's run by id.
"""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from devai.dashboard.routes import router
from devai.identity import Principal


class _FakeRedis:
    async def lrange(self, key, start, stop):
        return []


class _FakeState:
    def __init__(self, runs):
        self._runs = runs
        self.redis = _FakeRedis()

    async def get_run(self, run_id):
        return self._runs.get(run_id)

    async def get_agent_statuses(self, run_id):
        return {}

    async def list_runs(self, limit):
        return list(self._runs)

    async def list_runs_by_repo(self, repo, limit):
        return [rid for rid, r in self._runs.items() if r.get("repo") == repo]


class _FakeTeams:
    """Mirrors TeamService.can_dispatch: unscoped runs are open, scoped runs
    are members-only, and principals with no teams keep global access."""

    def can_dispatch(self, principal, team_id):
        if not team_id or principal is None or not principal.team_ids:
            return True
        return team_id in principal.team_ids


RUNS = {
    "run-alpha": {"run_id": "run-alpha", "team_id": "team-a", "repo": "o/a", "stage": "implement"},
    "run-beta": {"run_id": "run-beta", "team_id": "team-b", "repo": "o/b", "stage": "review"},
    "run-open": {"run_id": "run-open", "team_id": "", "repo": "o/c", "stage": "ingest"},
}


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    app.include_router(router)
    app.state.config = SimpleNamespace(require_auth=True)
    app.state.state_manager = _FakeState(dict(RUNS))
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


def test_member_reads_own_team_run(client):
    r = client.get("/dashboard/api/pipeline/runs/run-alpha", headers=_as("a@x.io", "team-a"))
    assert r.status_code == 200
    assert r.json()["run_id"] == "run-alpha"


def test_non_member_gets_404_on_other_team_run(client):
    r = client.get("/dashboard/api/pipeline/runs/run-beta", headers=_as("a@x.io", "team-a"))
    assert r.status_code == 404


def test_anonymous_is_rejected_when_require_auth_on(client):
    r = client.get("/dashboard/api/pipeline/runs/run-alpha")
    assert r.status_code == 401


def test_unscoped_run_stays_readable(client):
    r = client.get("/dashboard/api/pipeline/runs/run-open", headers=_as("a@x.io", "team-a"))
    assert r.status_code == 200


def test_missing_run_is_404(client):
    r = client.get("/dashboard/api/pipeline/runs/nope", headers=_as("a@x.io", "team-a"))
    assert r.status_code == 404


def test_run_list_omits_other_team_runs(client):
    r = client.get("/dashboard/api/pipeline/runs", headers=_as("a@x.io", "team-a"))
    assert r.status_code == 200
    assert [x["run_id"] for x in r.json()] == ["run-alpha", "run-open"]


def test_run_list_unscoped_principal_sees_all(client):
    r = client.get("/dashboard/api/pipeline/runs", headers=_as("ops@x.io"))
    assert r.status_code == 200
    assert len(r.json()) == 3
