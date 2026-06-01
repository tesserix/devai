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
