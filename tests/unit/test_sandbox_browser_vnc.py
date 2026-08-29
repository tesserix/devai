"""Human takeover reaches the agent's browser without exposing noVNC."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI, Response
from fastapi.testclient import TestClient

from devai.sandbox.models import SandboxRecord, SandboxSpec, SandboxStatus


def _record(*, owner: str = "dev@example.com", browser: bool = True) -> SandboxRecord:
    now = datetime.now(UTC)
    return SandboxRecord(
        id="sb-1",
        owner=owner,
        spec=SandboxSpec.model_validate(
            {
                "agent": {"name": "dev", "version": "1"},
                "model": {"provider": "anthropic", "model": "claude-sonnet-5"},
                "workspace": True,
                "browser": browser,
            }
        ),
        status=SandboxStatus.READY,
        created_at=now,
        expires_at=now + timedelta(hours=1),
        detail={"workspace": {"endpoint": "devai-sandbox-ws-sb-1.devai:8100"}},
    )


class _Service:
    def __init__(self, record: SandboxRecord) -> None:
        self.record = record

    async def get(self, sandbox_id: str, *, owner: str = "", is_admin: bool = False) -> Any:
        if sandbox_id != self.record.id or (not is_admin and owner != self.record.owner):
            return None
        return self.record

    async def touch(self, sandbox_id: str) -> None:
        return None


class _Runtime:
    async def read_secret_key(self, name: str, key: str) -> str:
        assert (name, key) == ("devai-sandbox-ws-sb-1", "token")
        return "capability-token"


def _api(record: SandboxRecord, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, list[tuple[str, str, str]]]:
    from devai.sandbox import routes

    seen: list[tuple[str, str, str]] = []

    async def fake_proxy(request: Any, endpoint: str, path: str, token: str) -> Response:
        seen.append((endpoint, path, token))
        return Response(content="desktop")

    monkeypatch.setattr(routes, "proxy_browser_request", fake_proxy)
    app = FastAPI()
    app.include_router(routes.router)
    app.state.sandbox_service = _Service(record)
    app.state.pipeline_service = SimpleNamespace(k8s_runtime=_Runtime())
    app.state.config = None
    return TestClient(app), seen


def test_owner_opens_novnc_through_the_capability_token(monkeypatch: pytest.MonkeyPatch) -> None:
    client, seen = _api(_record(), monkeypatch)

    response = client.get("/api/sandboxes/sb-1/browser/vnc.html", headers={"X-Forwarded-Email": "dev@example.com"})

    assert response.status_code == 200
    assert seen == [("devai-sandbox-ws-sb-1.devai:8100", "vnc.html", "capability-token")]


def test_another_owner_cannot_open_the_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    client, seen = _api(_record(), monkeypatch)

    response = client.get("/api/sandboxes/sb-1/browser/vnc.html", headers={"X-Forwarded-Email": "mallory@example.com"})

    assert response.status_code == 404
    assert seen == []


def test_plain_workspace_has_no_browser_desktop(monkeypatch: pytest.MonkeyPatch) -> None:
    client, seen = _api(_record(browser=False), monkeypatch)

    response = client.get("/api/sandboxes/sb-1/browser/vnc.html", headers={"X-Forwarded-Email": "dev@example.com"})

    assert response.status_code == 409
    assert seen == []


def test_workspace_desktop_proxy_requires_the_capability_token(monkeypatch: pytest.MonkeyPatch) -> None:
    from devai.sandbox import workspace_server

    async def fake_local(request: Any, path: str) -> Response:
        return Response(content=f"desktop:{path}")

    monkeypatch.setattr(workspace_server, "proxy_desktop_request", fake_local)
    client = TestClient(workspace_server.create_workspace_app(token="tok", browser=object()))

    assert client.get("/browser/desktop/vnc.html").status_code == 401
    assert client.get("/browser/desktop/vnc.html", headers={"X-DevAI-Workspace-Token": "tok"}).status_code == 200


def test_session_credentials_never_enter_the_browser_workspace() -> None:
    from devai.sandbox.browser_proxy import TOKEN_HEADER, browser_proxy_headers

    headers = browser_proxy_headers(
        {
            "Cookie": "devai_session=secret",
            "Authorization": "Bearer secret",
            "X-Forwarded-Email": "owner@example.com",
            "Accept": "text/html",
            "User-Agent": "browser",
        },
        token="workspace-capability",
    )

    assert headers == {
        "Accept": "text/html",
        "User-Agent": "browser",
        TOKEN_HEADER: "workspace-capability",
    }


def _socket_api(record: SandboxRecord, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from devai.sandbox import routes

    async def fake_socket(websocket: Any, endpoint: str, path: str, token: str) -> None:
        await websocket.accept()
        await websocket.send_text(f"{endpoint}:{path}:{token}")
        await websocket.close()

    monkeypatch.setattr(routes, "proxy_browser_socket", fake_socket)
    app = FastAPI()
    app.include_router(routes.router)
    app.state.sandbox_service = _Service(record)
    app.state.pipeline_service = SimpleNamespace(k8s_runtime=_Runtime())
    app.state.config = None
    return TestClient(app)


def test_same_origin_owner_can_open_the_browser_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _socket_api(_record(), monkeypatch)

    with client.websocket_connect(
        "/api/sandboxes/sb-1/browser/websockify",
        headers={"Origin": "http://testserver", "X-Forwarded-Email": "dev@example.com"},
    ) as websocket:
        assert websocket.receive_text().endswith("websockify:capability-token")


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://evil.example", "X-Forwarded-Email": "dev@example.com"},
        {"Origin": "http://testserver", "X-Forwarded-Email": "mallory@example.com"},
    ],
)
def test_foreign_origin_or_owner_cannot_open_the_browser_socket(
    headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from starlette.websockets import WebSocketDisconnect

    client = _socket_api(_record(), monkeypatch)

    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect("/api/sandboxes/sb-1/browser/websockify", headers=headers) as websocket,
    ):
        websocket.receive_text()
