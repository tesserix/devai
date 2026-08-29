from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from devai.dashboard.routes import router
from devai.identity import Principal


class _FakeSCM:
    def __init__(self) -> None:
        self.issues: dict[int, dict] = {}
        self.comments: dict[int, list[dict]] = {}
        self.next_number = 1

    async def create_issue(self, repo, title, body, labels=None):
        number = self.next_number
        self.next_number += 1
        issue = {
            "number": number,
            "title": title,
            "body": body,
            "labels": [{"name": label} for label in labels or []],
            "state": "open",
            "html_url": f"https://github.test/{repo}/issues/{number}",
            "created_at": "2026-08-29T00:00:00Z",
            "updated_at": "2026-08-29T00:00:00Z",
        }
        self.issues[number] = issue
        self.comments[number] = []
        return deepcopy(issue)

    async def assign_issue(self, repo, issue_number, assignees):
        return {}

    async def list_issues(self, repo, state="open", labels=None, limit=100):
        issues = list(self.issues.values())
        if state != "all":
            issues = [issue for issue in issues if issue["state"] == state]
        return deepcopy(issues[:limit])

    async def get_issue(self, repo, issue_id):
        return deepcopy(self.issues[int(issue_id)])

    async def list_issue_comments(self, repo, issue_id, limit=100):
        return deepcopy(self.comments[int(issue_id)][:limit])

    async def add_comment(self, repo, issue_id, body):
        comment = {
            "id": len(self.comments[int(issue_id)]) + 1,
            "body": body,
            "html_url": f"https://github.test/{repo}/issues/{issue_id}#comment-1",
            "created_at": "2026-08-29T00:01:00Z",
            "user": {"login": "devai-gh-app[bot]"},
        }
        self.comments[int(issue_id)].append(comment)
        self.issues[int(issue_id)]["updated_at"] = comment["created_at"]
        return deepcopy(comment)

    async def update_issue(self, repo, issue_id, **changes):
        self.issues[int(issue_id)].update({key: value for key, value in changes.items() if value is not None})
        return deepcopy(self.issues[int(issue_id)])

    async def close(self):
        return None


@pytest.fixture
def feedback_app(monkeypatch):
    scm = _FakeSCM()
    principals = {
        "alice": Principal(email="alice@example.com", uid="alice-uid", tenant_id="tenant-a"),
        "alice-other-tenant": Principal(email="alice@example.com", uid="alice-uid", tenant_id="tenant-b"),
        "bob": Principal(email="bob@example.com", uid="bob-uid", tenant_id="tenant-b"),
        "support": Principal(
            email="support@example.com",
            uid="support-uid",
            roles=["support-engineer"],
        ),
    }

    async def principal(request):
        selected = request.headers.get("x-test-principal", "")
        if selected not in principals:
            raise RuntimeError("test principal missing")
        return principals[selected]

    async def no_rate_limit(request, bucket, resolved_principal):
        return None

    monkeypatch.setattr("devai.dashboard.routes._require_principal", principal)
    monkeypatch.setattr("devai.dashboard.routes.enforce_rate_limit", no_rate_limit)
    monkeypatch.setattr("devai.dashboard.routes.create_scm_client", lambda config: scm)

    app = FastAPI()
    app.include_router(router)
    app.state.config = SimpleNamespace(
        feedback_repo="tesserix/devai",
        feedback_assignees=["owner-login"],
    )
    return TestClient(app), scm


def _as(name: str) -> dict[str, str]:
    return {"x-test-principal": name}


def _create(client: TestClient, principal: str, title: str = "Agent import issue") -> dict:
    response = client.post(
        "/dashboard/api/feedback",
        headers=_as(principal),
        json={"type": "bug", "title": title, "description": "The import fails after validation."},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_feedback_always_requires_an_authenticated_principal(feedback_app, monkeypatch):
    client, _scm = feedback_app

    async def anonymous(request):
        return None

    monkeypatch.setattr("devai.dashboard.routes._require_principal", anonymous)

    assert client.get("/dashboard/api/feedback").status_code == 401


def test_submit_and_list_are_scoped_to_the_exact_authenticated_owner(feedback_app):
    client, _scm = feedback_app
    alice = _create(client, "alice", "Alice issue")
    _create(client, "bob", "Bob issue")

    response = client.get("/dashboard/api/feedback", headers=_as("alice"))

    assert response.status_code == 200
    assert response.json()["can_manage"] is False
    assert [thread["id"] for thread in response.json()["threads"]] == [alice["id"]]
    assert response.json()["threads"][0]["status"] == "open"


def test_cross_user_and_cross_tenant_detail_is_indistinguishable_from_missing(feedback_app):
    client, _scm = feedback_app
    alice = _create(client, "alice")

    response = client.get(f"/dashboard/api/feedback/{alice['id']}", headers=_as("bob"))
    other_tenant = client.get(
        f"/dashboard/api/feedback/{alice['id']}",
        headers=_as("alice-other-tenant"),
    )

    assert response.status_code == 404
    assert other_tenant.status_code == 404


def test_legacy_feedback_remains_visible_to_its_exact_submitter(feedback_app):
    client, scm = feedback_app
    scm.issues[323] = {
        "number": 323,
        "title": "[story] Existing feedback",
        "body": "## User Story\n\nOriginal request\n\n---\nSubmitted by: alice@example.com\nLegacy form.",
        "labels": [{"name": "feedback"}, {"name": "type:story"}],
        "state": "closed",
        "html_url": "https://github.test/tesserix/devai/issues/323",
        "created_at": "2026-08-28T00:00:00Z",
        "updated_at": "2026-08-29T00:00:00Z",
    }
    scm.comments[323] = []

    response = client.get("/dashboard/api/feedback", headers=_as("alice"))

    assert response.status_code == 200
    assert response.json()["threads"][0]["id"] == "323"
    assert response.json()["threads"][0]["status"] == "closed"


def test_user_can_reply_and_read_the_conversation_while_open(feedback_app):
    client, _scm = feedback_app
    thread = _create(client, "alice")

    reply = client.post(
        f"/dashboard/api/feedback/{thread['id']}/replies",
        headers=_as("alice"),
        json={"message": "This also happens with a second ADK."},
    )
    detail = client.get(f"/dashboard/api/feedback/{thread['id']}", headers=_as("alice"))

    assert reply.status_code == 200
    assert reply.json()["author"] == "alice@example.com"
    assert reply.json()["author_role"] == "user"
    assert detail.status_code == 200
    assert detail.json()["replies"][0]["body"] == "This also happens with a second ADK."


def test_only_support_can_close_and_closed_threads_reject_user_replies(feedback_app):
    client, _scm = feedback_app
    thread = _create(client, "alice")

    forbidden = client.patch(
        f"/dashboard/api/feedback/{thread['id']}/status",
        headers=_as("alice"),
        json={"status": "closed"},
    )
    closed = client.patch(
        f"/dashboard/api/feedback/{thread['id']}/status",
        headers=_as("support"),
        json={"status": "closed"},
    )
    reply = client.post(
        f"/dashboard/api/feedback/{thread['id']}/replies",
        headers=_as("alice"),
        json={"message": "One more detail"},
    )

    assert forbidden.status_code == 403
    assert closed.status_code == 200
    assert closed.json()["status"] == "closed"
    assert reply.status_code == 409


def test_support_lists_and_replies_to_every_users_threads(feedback_app):
    client, _scm = feedback_app
    alice = _create(client, "alice", "Alice issue")
    _create(client, "bob", "Bob issue")

    listing = client.get("/dashboard/api/feedback", headers=_as("support"))
    reply = client.post(
        f"/dashboard/api/feedback/{alice['id']}/replies",
        headers=_as("support"),
        json={"message": "We are investigating this."},
    )

    assert listing.status_code == 200
    assert listing.json()["can_manage"] is True
    assert len(listing.json()["threads"]) == 2
    assert reply.status_code == 200
    assert reply.json()["author_role"] == "support"


def test_assigned_owner_is_treated_as_support(feedback_app, monkeypatch):
    client, _scm = feedback_app
    monkeypatch.setitem(
        client.app.state.config.__dict__,
        "feedback_assignees",
        ["owner-login"],
    )

    # The direct GitHub login is the stable uid for OAuth principals.
    owner = Principal(email="owner@example.com", uid="owner-login")

    async def owner_principal(request):
        return owner

    monkeypatch.setattr("devai.dashboard.routes._require_principal", owner_principal)
    response = client.get("/dashboard/api/feedback", headers=_as("support"))

    assert response.status_code == 200
    assert response.json()["can_manage"] is True
