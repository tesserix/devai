"""Browser control stays inside one capability-token-gated workspace."""

from __future__ import annotations

import pytest

from devai.sandbox.browser import BrowserDownloads, BrowserError, validate_browser_url
from devai.sandbox.browser_desktop import desktop_commands
from devai.sandbox.workspace import BROWSER_PORT


def _addresses(*values: str):
    return lambda _host: values


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/file", "javascript:alert(1)"])
def test_browser_navigation_accepts_only_http_and_https(url: str) -> None:
    with pytest.raises(BrowserError, match="http"):
        validate_browser_url(url, resolve=_addresses("8.8.8.8"))


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.0.0.4", "169.254.169.254", "172.16.0.1", "192.168.1.1", "::1", "fc00::1"],
)
def test_browser_navigation_blocks_private_and_metadata_addresses(address: str) -> None:
    with pytest.raises(BrowserError, match="private|internal"):
        validate_browser_url("https://allowed.example", resolve=_addresses(address))


def test_browser_navigation_allows_public_addresses() -> None:
    assert validate_browser_url("https://example.com/path", resolve=_addresses("8.8.8.8")) == (
        "https://example.com/path"
    )


@pytest.mark.parametrize("url", ["http://localhost:3000", "http://127.0.0.1:4173"])
def test_browser_navigation_allows_the_workspace_own_preview(url: str) -> None:
    assert validate_browser_url(url, resolve=_addresses("127.0.0.1")) == url


def test_browser_navigation_rejects_credentials_in_urls() -> None:
    with pytest.raises(BrowserError, match="credentials"):
        validate_browser_url("https://user:secret@example.com", resolve=_addresses("8.8.8.8"))


def test_novnc_binds_only_to_workspace_loopback() -> None:
    commands = desktop_commands()
    x11vnc = next(command for command in commands if command[0] == "x11vnc")
    websockify = next(command for command in commands if command[0] == "websockify")

    assert "-localhost" in x11vnc
    assert f"127.0.0.1:{BROWSER_PORT}" in websockify
    assert all("unconfined" not in argument.lower() for command in commands for argument in command)


class _Download:
    suggested_filename = "../../report.txt"

    async def save_as(self, path: str) -> None:
        from pathlib import Path

        Path(path).write_text("downloaded")


@pytest.mark.asyncio
async def test_browser_download_is_saved_inside_the_shared_workspace(tmp_path) -> None:
    downloads = BrowserDownloads(tmp_path)

    relative_path = await downloads.save(_Download())

    assert relative_path == "report.txt"
    assert (tmp_path / relative_path).read_text() == "downloaded"


class _Browser:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def navigate(self, url: str):
        self.calls.append(("navigate", url))
        return {"url": url, "title": "Example", "status": 200}

    async def screenshot(self, *, full_page: bool = False):
        self.calls.append(("screenshot", full_page))
        return {"media_type": "image/png", "base64": "aW1hZ2U="}

    async def click(self, selector: str):
        self.calls.append(("click", selector))
        return {"clicked": selector}

    async def type(self, selector: str, text: str):
        self.calls.append(("type", (selector, text)))
        return {"typed": selector}

    async def scroll(self, *, delta_x: float = 0, delta_y: float = 600):
        self.calls.append(("scroll", (delta_x, delta_y)))
        return {"delta_x": delta_x, "delta_y": delta_y}

    async def get_content(self, *, selector: str = ""):
        self.calls.append(("content", selector))
        return {"content": "Example"}


class _LifecycleBrowser(_Browser):
    def __init__(self) -> None:
        super().__init__()
        self.started = False
        self.closed = False

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.closed = True


class _FailingBrowser(_Browser):
    async def navigate(self, url: str):
        raise RuntimeError("internal browser detail")


def _workspace_client(browser: _Browser | None = None):
    from fastapi.testclient import TestClient

    from devai.sandbox.workspace_server import create_workspace_app

    return TestClient(create_workspace_app(token="tok", browser=browser))


def test_browser_control_requires_the_workspace_capability_token() -> None:
    client = _workspace_client(_Browser())

    assert client.post("/browser/navigate", json={"url": "https://example.com"}).status_code == 401


def test_browser_control_is_absent_from_a_plain_workspace() -> None:
    response = _workspace_client().post(
        "/browser/navigate",
        json={"url": "https://example.com"},
        headers={"X-DevAI-Workspace-Token": "tok"},
    )

    assert response.status_code == 409


def test_browser_control_routes_drive_the_same_browser_session() -> None:
    browser = _Browser()
    client = _workspace_client(browser)
    headers = {"X-DevAI-Workspace-Token": "tok"}

    assert client.post("/browser/navigate", json={"url": "https://example.com"}, headers=headers).status_code == 200
    assert client.post("/browser/screenshot", json={"full_page": True}, headers=headers).status_code == 200
    assert client.post("/browser/click", json={"selector": "#submit"}, headers=headers).status_code == 200
    assert client.post("/browser/type", json={"selector": "#name", "text": "Ada"}, headers=headers).status_code == 200
    assert client.post("/browser/scroll", json={"delta_y": 900}, headers=headers).status_code == 200
    assert client.post("/browser/content", json={"selector": "main"}, headers=headers).status_code == 200

    assert [operation for operation, _ in browser.calls] == [
        "navigate",
        "screenshot",
        "click",
        "type",
        "scroll",
        "content",
    ]


def test_browser_is_live_for_vnc_for_the_whole_workspace_lifecycle() -> None:
    browser = _LifecycleBrowser()

    with _workspace_client(browser):
        assert browser.started is True

    assert browser.closed is True


def test_browser_dependency_errors_are_bounded_and_redacted() -> None:
    response = _workspace_client(_FailingBrowser()).post(
        "/browser/navigate",
        json={"url": "https://example.com"},
        headers={"X-DevAI-Workspace-Token": "tok"},
    )

    assert response.status_code == 502
    assert response.json()["detail"] == "browser operation failed"
    assert "internal browser detail" not in response.text
