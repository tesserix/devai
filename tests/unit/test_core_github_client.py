from __future__ import annotations

import httpx
import pytest

from devai.config import Settings
from devai.core.github_client import GITHUB_LABEL_MAX_LENGTH, GitHubClient


def _http_error(method: str, path: str, status_code: int, payload: dict) -> httpx.HTTPStatusError:
    request = httpx.Request(method, f"https://api.github.com{path}")
    response = httpx.Response(status_code, json=payload, request=request)
    return httpx.HTTPStatusError("GitHub error", request=request, response=response)


@pytest.mark.asyncio
async def test_create_issue_retries_without_labels_after_label_validation_error() -> None:
    client = GitHubClient(Settings())
    calls: list[tuple[str, str, dict | None]] = []

    async def fake_request(method: str, path: str, **kwargs: object) -> httpx.Response:
        payload = kwargs.get("json")
        calls.append((method, path, payload if isinstance(payload, dict) else None))

        if path == "/repos/tesserix/MY-APP/labels":
            return httpx.Response(201, json={"name": payload["name"]}, request=httpx.Request(method, path))

        if path == "/repos/tesserix/MY-APP/issues" and isinstance(payload, dict) and "labels" in payload:
            raise _http_error(
                method,
                path,
                422,
                {
                    "message": "Validation Failed",
                    "errors": [{"field": "labels", "code": "invalid"}],
                },
            )

        if path == "/repos/tesserix/MY-APP/issues":
            return httpx.Response(
                201,
                json={"number": 42, "html_url": "https://github.com/tesserix/MY-APP/issues/42"},
                request=httpx.Request(method, path),
            )

        raise AssertionError(f"Unexpected request: {method} {path}")

    client._request = fake_request  # type: ignore[method-assign]

    issue = await client.create_issue(
        repo="tesserix/MY-APP",
        title="Epic title",
        body="Epic body",
        labels=["epic", "devai:epic"],
    )

    assert issue["number"] == 42
    issue_posts = [payload for method, path, payload in calls if method == "POST" and path.endswith("/issues")]
    assert issue_posts[0] == {"title": "Epic title", "body": "Epic body", "labels": ["epic", "devai:epic"]}
    assert issue_posts[1] == {"title": "Epic title", "body": "Epic body"}

    await client.close()


@pytest.mark.asyncio
async def test_ensure_labels_normalizes_and_skips_invalid_labels() -> None:
    client = GitHubClient(Settings())
    created_labels: list[str] = []

    async def fake_request(method: str, path: str, **kwargs: object) -> httpx.Response:
        payload = kwargs.get("json")
        if path != "/repos/tesserix/MY-APP/labels" or not isinstance(payload, dict):
            raise AssertionError(f"Unexpected request: {method} {path}")

        label = payload["name"]
        created_labels.append(label)
        if label == "bad-label":
            raise _http_error(
                method,
                path,
                422,
                {
                    "message": "Validation Failed",
                    "errors": [{"field": "name", "code": "invalid"}],
                },
            )
        return httpx.Response(201, json={"name": label}, request=httpx.Request(method, path))

    client._request = fake_request  # type: ignore[method-assign]

    labels = await client.ensure_labels(
        "tesserix/MY-APP",
        ["  epic  ", "epic", "", "bad-label", "x" * (GITHUB_LABEL_MAX_LENGTH + 20)],
    )

    assert labels == ["epic", "x" * GITHUB_LABEL_MAX_LENGTH]
    assert created_labels == ["epic", "bad-label", "x" * GITHUB_LABEL_MAX_LENGTH]

    await client.close()
