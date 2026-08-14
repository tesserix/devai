"""Reading the kit's release list from GitHub."""

from __future__ import annotations

import httpx
import pytest
import respx

from devai.kit.versions import ADK_REPO, create_adk_catalogue, github_releases


@respx.mock
async def test_reads_the_release_list_for_the_kit_repo():
    route = respx.get(f"https://api.github.com/repos/{ADK_REPO}/releases").mock(
        return_value=httpx.Response(200, json=[{"tag_name": "v0.1.1", "draft": False, "prerelease": False}])
    )

    assert await github_releases(ADK_REPO, token="")() == [{"tag_name": "v0.1.1", "draft": False, "prerelease": False}]
    assert route.called


@respx.mock
async def test_sends_the_token_when_one_is_configured():
    route = respx.get(f"https://api.github.com/repos/{ADK_REPO}/releases").mock(
        return_value=httpx.Response(200, json=[])
    )

    await github_releases(ADK_REPO, token="ghp_secret")()

    assert route.calls.last.request.headers["authorization"] == "Bearer ghp_secret"


@respx.mock
async def test_omits_the_header_when_no_token_is_configured():
    route = respx.get(f"https://api.github.com/repos/{ADK_REPO}/releases").mock(
        return_value=httpx.Response(200, json=[])
    )

    await github_releases(ADK_REPO, token="")()

    assert "authorization" not in route.calls.last.request.headers


@respx.mock
async def test_an_error_response_raises_so_the_catalogue_falls_back():
    respx.get(f"https://api.github.com/repos/{ADK_REPO}/releases").mock(return_value=httpx.Response(403))

    with pytest.raises(httpx.HTTPStatusError):
        await github_releases(ADK_REPO, token="")()


@respx.mock
async def test_the_catalogue_from_settings_offers_the_repo_releases():
    respx.get(f"https://api.github.com/repos/{ADK_REPO}/releases").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"tag_name": "v0.1.1", "draft": False, "prerelease": False},
                {"tag_name": "v0.1.0", "draft": False, "prerelease": False},
            ],
        )
    )

    class _Settings:
        scm_token = ""

    catalogue = create_adk_catalogue(_Settings())

    assert await catalogue.versions() == ["0.1.1", "0.1.0"]
