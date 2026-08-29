"""GitHub SCM client: batch marker probe (GraphQL) + reviewer request."""

from __future__ import annotations

import httpx
import pytest

from devai.scm.base import AuthMethod
from devai.scm.github_client import GitHubSCMClient


@pytest.mark.asyncio
async def test_probe_markers_batches_into_one_graphql_call() -> None:
    client = GitHubSCMClient(auth_method=AuthMethod.PAT, token="t")
    captured: dict[str, object] = {}

    async def fake_graphql(query: str, variables: dict | None = None):
        captured["query"] = query
        captured["variables"] = variables
        # r0 = tesserix/a has the object; r1 = tesserix/b does not.
        return {
            "r0": {"object": {"__typename": "Blob"}},
            "r1": {"object": None},
        }

    client._graphql = fake_graphql  # type: ignore[assignment]

    out = await client.probe_markers(
        [("tesserix", "a", "main"), ("tesserix", "b", "main")],
        ".platform/devai.yaml",
    )
    assert out == {"tesserix/a": True, "tesserix/b": False}
    # One aliased query carries both repos.
    assert "r0: repository" in captured["query"]
    assert "r1: repository" in captured["query"]
    assert captured["variables"]["e0"] == "main:.platform/devai.yaml"
    await client.close()


@pytest.mark.asyncio
async def test_probe_markers_bad_batch_omits_unknown_repos() -> None:
    # A failed GraphQL batch must NOT report repos as marker-absent — an
    # "unknown" verdict read as "marker gone" would false-offload onboarded
    # repos to DORMANT on the next reconcile. Failed repos are omitted, and
    # callers treat a missing key as "no change".
    client = GitHubSCMClient(auth_method=AuthMethod.PAT, token="t")

    async def boom(query: str, variables: dict | None = None):
        raise RuntimeError("GraphQL error")

    client._graphql = boom  # type: ignore[assignment]
    out = await client.probe_markers([("tesserix", "a", "main")], ".platform/devai.yaml")
    assert out == {}
    await client.close()


@pytest.mark.asyncio
async def test_request_reviewers_splits_users_and_teams() -> None:
    client = GitHubSCMClient(auth_method=AuthMethod.PAT, token="t")
    captured: dict[str, object] = {}

    async def fake_request(method: str, path: str, **kwargs):
        captured["method"] = method
        captured["path"] = path
        captured["json"] = kwargs.get("json")
        return httpx.Response(
            201,
            json={"requested_reviewers": [{"login": "mahesh"}]},
            request=httpx.Request(method, path),
        )

    client._request = fake_request  # type: ignore[assignment]

    out = await client.request_reviewers("tesserix/devai", 7, ["@mahesh", "tesserix/platform"])
    assert captured["path"] == "/repos/tesserix/devai/pulls/7/requested_reviewers"
    assert captured["json"] == {"reviewers": ["mahesh"], "team_reviewers": ["platform"]}
    assert out == {"requested": ["mahesh"]}
    await client.close()


def _repo(full_name: str, **extra) -> dict:
    owner, name = full_name.split("/")
    return {
        "full_name": full_name,
        "name": name,
        "owner": {"login": owner},
        "default_branch": "main",
        **extra,
    }


@pytest.mark.asyncio
async def test_list_installation_repos_pat_uses_org_scoped_endpoint() -> None:
    # A PAT bound to an org must query the cheap org-scoped listing rather than
    # walking every org the token can see (the slow path that tripped the edge
    # gateway timeout and served a raw 502 to the Repos page).
    client = GitHubSCMClient(auth_method=AuthMethod.PAT, token="t", org="tesserix")
    paths: list[str] = []

    async def fake_request(method: str, path: str, **kwargs):
        paths.append(path)
        return httpx.Response(200, json=[_repo("tesserix/devai")], request=httpx.Request(method, path))

    client._request = fake_request  # type: ignore[assignment]
    out = await client.list_installation_repos(per_page=100)

    assert len(paths) == 1
    assert paths[0].startswith("/orgs/tesserix/repos")
    assert [r["full_name"] for r in out] == ["tesserix/devai"]
    await client.close()


@pytest.mark.asyncio
async def test_list_installation_repos_falls_back_to_user_repos_on_404() -> None:
    # If the configured "org" is actually a user account, /orgs/{org}/repos
    # 404s — we must fall back to /user/repos (scoped to the owner) instead of
    # surfacing a hard error.
    client = GitHubSCMClient(auth_method=AuthMethod.PAT, token="t", org="someuser")
    paths: list[str] = []

    async def fake_request(method: str, path: str, **kwargs):
        paths.append(path)
        if path.startswith("/orgs/"):
            req = httpx.Request(method, path)
            raise httpx.HTTPStatusError("not found", request=req, response=httpx.Response(404, request=req))
        # /user/repos spans orgs — include one foreign repo to prove filtering.
        return httpx.Response(
            200,
            json=[_repo("someuser/app"), _repo("otherorg/lib")],
            request=httpx.Request(method, path),
        )

    client._request = fake_request  # type: ignore[assignment]
    out = await client.list_installation_repos(per_page=100)

    assert paths[0].startswith("/orgs/someuser/repos")
    assert any(p.startswith("/user/repos") for p in paths)
    # Only the bound owner's repo survives the filter.
    assert [r["full_name"] for r in out] == ["someuser/app"]
    await client.close()


@pytest.mark.asyncio
async def test_list_installation_repos_github_app_uses_installation_endpoint() -> None:
    client = GitHubSCMClient(auth_method=AuthMethod.GITHUB_APP, org="tesserix")
    paths: list[str] = []

    async def fake_request(method: str, path: str, **kwargs):
        paths.append(path)
        return httpx.Response(
            200,
            json={"repositories": [_repo("tesserix/devai")]},
            request=httpx.Request(method, path),
        )

    client._request = fake_request  # type: ignore[assignment]
    out = await client.list_installation_repos(per_page=100)

    assert paths[0].startswith("/installation/repositories")
    assert [r["full_name"] for r in out] == ["tesserix/devai"]
    await client.close()


@pytest.mark.asyncio
async def test_list_issues_paginates_to_the_requested_limit() -> None:
    client = GitHubSCMClient(auth_method=AuthMethod.PAT, token="t")
    pages: list[int] = []

    async def fake_request(method: str, path: str, **kwargs):
        page = int(kwargs["params"]["page"])
        pages.append(page)
        count = 100 if page < 3 else 25
        issues = [
            {"number": (page - 1) * 100 + index, "title": "Feedback", "labels": []} for index in range(1, count + 1)
        ]
        return httpx.Response(200, json=issues, request=httpx.Request(method, path))

    client._request = fake_request  # type: ignore[assignment]
    issues = await client.list_issues("tesserix/devai", state="all", labels=["feedback"], limit=225)

    assert pages == [1, 2, 3]
    assert len(issues) == 225
    await client.close()


@pytest.mark.asyncio
async def test_list_issue_comments_paginates_in_chronological_order() -> None:
    client = GitHubSCMClient(auth_method=AuthMethod.PAT, token="t")
    pages: list[int] = []

    async def fake_request(method: str, path: str, **kwargs):
        page = int(kwargs["params"]["page"])
        pages.append(page)
        count = 100 if page == 1 else 1
        comments = [{"id": (page - 1) * 100 + index} for index in range(1, count + 1)]
        return httpx.Response(200, json=comments, request=httpx.Request(method, path))

    client._request = fake_request  # type: ignore[assignment]
    comments = await client.list_issue_comments("tesserix/devai", 323, limit=200)

    assert pages == [1, 2]
    assert [comment["id"] for comment in comments] == list(range(1, 102))
    await client.close()
