"""Looking at what the agent built, before anything merges (#206).

Every other piece of the eval stack produces a number. The preview produces the
thing the number is an index into — so it has to be reachable, and reachable
only by whoever owns the sandbox.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from devai.sandbox.preview import WORKSPACE_INTERNAL_PORTS, listening_ports

# Two listeners on 0.0.0.0: 0x1F90 = 8080, 0x0FA0 = 4000; the third is a client
# socket (state 01 = ESTABLISHED) and must not be offered as a preview.
_PROC_NET_TCP = """  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 00000000:1F90 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 12345 1 0000 100 0 0 10 0
   1: 00000000:0FA0 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 12346 1 0000 100 0 0 10 0
   2: 0100007F:A1B2 0100007F:1F90 01 00000000:00000000 00:00000000 00000000  1000        0 12347 1 0000 100 0 0 10 0
"""

_PROC_NET_TCP_WORKSPACE_ONLY = """  sl  local_address rem_address   st
   0: 00000000:1FA4 00000000:0000 0A
"""


def test_only_listening_sockets_count_as_a_preview() -> None:
    assert listening_ports(_PROC_NET_TCP) == [4000, 8080]


def test_the_workspaces_own_port_is_not_a_preview() -> None:
    assert 8100 in WORKSPACE_INTERNAL_PORTS
    assert listening_ports(_PROC_NET_TCP_WORKSPACE_ONLY) == []


def test_an_unreadable_proc_table_yields_no_ports_rather_than_an_error() -> None:
    assert listening_ports("") == []
    assert listening_ports("garbage\nnot: a:table") == []


# ── the workspace side ────────────────────────────────────────────────


def _workspace_client(monkeypatch: pytest.MonkeyPatch, *, ports: list[int]) -> TestClient:
    from devai.sandbox import workspace_server

    monkeypatch.setattr(workspace_server, "detect_ports", lambda: ports)
    return TestClient(workspace_server.create_workspace_app(token="tok"))


def test_the_workspace_reports_the_ports_the_agent_opened(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _workspace_client(monkeypatch, ports=[3000])
    resp = client.get("/preview/ports", headers={"X-DevAI-Workspace-Token": "tok"})
    assert resp.status_code == 200
    assert resp.json() == {"ports": [3000]}


def test_the_port_list_needs_the_capability_token(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _workspace_client(monkeypatch, ports=[3000])
    assert client.get("/preview/ports").status_code == 401


# ── the API side ──────────────────────────────────────────────────────


class _StubWorkspaceClient:
    def __init__(self) -> None:
        self.fetched: list[tuple[int, str]] = []

    async def preview_ports(self) -> list[int]:
        return [3000]

    async def preview_fetch(self, port: int, path: str) -> dict[str, Any]:
        self.fetched.append((port, path))
        return {"status": 200, "body": "<h1>it built</h1>", "content_type": "text/html"}


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, _StubWorkspaceClient]:
    from fastapi import FastAPI

    from devai.sandbox import routes

    stub = _StubWorkspaceClient()

    async def fake_client(request: Any, record: Any) -> Any:
        return stub

    monkeypatch.setattr(routes, "_workspace_client", fake_client)

    app = FastAPI()
    app.include_router(routes.router)
    app.state.sandbox_service = _StubService()
    app.state.config = None
    return TestClient(app), stub


class _StubService:
    def __init__(self) -> None:
        self.known = {"sb-1"}

    async def get(self, sandbox_id: str, *, owner: str = "", is_admin: bool = False) -> Any:
        if sandbox_id not in self.known:
            return None
        from datetime import UTC, datetime, timedelta

        from devai.sandbox.models import SandboxRecord, SandboxSpec, SandboxStatus

        now = datetime.now(UTC)
        return SandboxRecord(
            id=sandbox_id,
            owner="dev@example.com",
            spec=SandboxSpec.model_validate(
                {
                    "agent": {"name": "dev", "version": "1"},
                    "model": {"provider": "anthropic", "model": "claude-sonnet-5"},
                    "workspace": True,
                }
            ),
            status=SandboxStatus.READY,
            created_at=now,
            expires_at=now + timedelta(hours=1),
        )

    async def touch(self, sandbox_id: str) -> None:
        return None


def test_the_api_lists_what_can_be_previewed(api: tuple[TestClient, _StubWorkspaceClient]) -> None:
    client, _ = api
    resp = client.get("/api/sandboxes/sb-1/preview")
    assert resp.status_code == 200
    assert resp.json() == {"ports": [3000]}


def test_a_preview_serves_the_app_the_agent_just_built(api: tuple[TestClient, _StubWorkspaceClient]) -> None:
    client, stub = api
    resp = client.get("/api/sandboxes/sb-1/preview/3000/index.html")
    assert resp.status_code == 200
    assert "it built" in resp.text
    assert stub.fetched == [(3000, "index.html")]


def test_a_preview_of_a_sandbox_you_do_not_own_is_not_found(api: tuple[TestClient, _StubWorkspaceClient]) -> None:
    client, _ = api
    assert client.get("/api/sandboxes/sb-other/preview").status_code == 404
    assert client.get("/api/sandboxes/sb-other/preview/3000/").status_code == 404
