"""Human takeover of a running workspace (#206).

The point of the takeover is that nothing is recreated: a person opens the tree
in the state the agent left it. That only holds if the IDE runs *in* the
workspace pod, on the same volume — and if nothing else in the namespace,
including another sandbox, can reach it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from devai.sandbox.isolation import build_isolation_manifests
from devai.sandbox.models import SandboxRecord, SandboxSpec, SandboxStatus
from devai.sandbox.workspace import IDE_PORT, WORKSPACE_ROOT, build_workspace_manifests


def _record(**spec_extra: Any) -> SandboxRecord:
    now = datetime.now(UTC)
    return SandboxRecord(
        id="sb-1",
        owner="dev@example.com",
        spec=SandboxSpec.model_validate(
            {
                "agent": {"name": "dev", "version": "1"},
                "model": {"provider": "anthropic", "model": "claude-sonnet-5"},
                "workspace": True,
                **spec_extra,
            }
        ),
        status=SandboxStatus.READY,
        created_at=now,
        expires_at=now + timedelta(hours=1),
    )


def _manifests(record: SandboxRecord) -> dict[str, dict[str, Any]]:
    return {m["kind"]: m for m in build_workspace_manifests(record, namespace="devai", token="tok")}


def test_a_workspace_without_takeover_runs_no_ide() -> None:
    pod = _manifests(_record())["Pod"]["spec"]
    assert [c["name"] for c in pod["containers"]] == ["workspace"]


def test_the_ide_edits_the_same_tree_the_agent_left() -> None:
    pod = _manifests(_record(ide=True))["Pod"]["spec"]
    ide = next(c for c in pod["containers"] if c["name"] == "ide")

    assert ide["volumeMounts"] == pod["containers"][0]["volumeMounts"]
    assert WORKSPACE_ROOT in " ".join(ide["args"])


def test_the_ide_is_reachable_on_the_workspace_service() -> None:
    ports = _manifests(_record(ide=True))["Service"]["spec"]["ports"]
    assert IDE_PORT in [p["port"] for p in ports]


def test_the_ide_url_points_at_the_ide_port_of_this_sandbox() -> None:
    from devai.sandbox.ide_proxy import ide_upstream_url

    url = ide_upstream_url("devai-sandbox-ws-sb-1.devai.svc.cluster.local:8100", "static/out.js")
    assert url == f"http://devai-sandbox-ws-sb-1.devai.svc.cluster.local:{IDE_PORT}/static/out.js"


def test_no_other_sandbox_can_reach_this_one() -> None:
    """The IDE trusts whoever reaches it, so only the control plane may."""
    policy = next(
        m
        for m in build_isolation_manifests(_record(ide=True), namespace="devai-sbx-sb-1")
        if m["kind"] == "NetworkPolicy"
    )

    assert "Ingress" in policy["spec"]["policyTypes"]
    allowed = policy["spec"]["ingress"][0]["from"]

    # Only the control-plane pods cross the namespace boundary; the bare
    # podSelector admits this sandbox's own namespace, which no other sandbox
    # shares — so another sandbox matches nothing.
    assert {
        "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "devai"}},
        "podSelector": {"matchLabels": {"app.kubernetes.io/name": "devai"}},
    } in allowed
    assert {"podSelector": {}} in allowed
    assert all("namespaceSelector" in rule or rule["podSelector"] == {} for rule in allowed)


# ── the API in front of it ────────────────────────────────────────────


class _StubService:
    def __init__(self, record: SandboxRecord) -> None:
        self.record = record

    async def get(self, sandbox_id: str, *, owner: str = "", is_admin: bool = False) -> Any:
        return self.record if sandbox_id == self.record.id else None

    async def touch(self, sandbox_id: str) -> None:
        return None


def _api(record: SandboxRecord, monkeypatch: Any) -> Any:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from devai.sandbox import routes

    async def fake_proxy(request: Any, endpoint: str, path: str) -> Any:
        from fastapi import Response

        return Response(content=f"ide:{endpoint}:{path}", media_type="text/html")

    monkeypatch.setattr(routes, "proxy_ide_request", fake_proxy)
    app = FastAPI()
    app.include_router(routes.router)
    app.state.sandbox_service = _StubService(record)
    app.state.config = None
    return TestClient(app)


def _ready(**spec_extra: Any) -> SandboxRecord:
    record = _record(**spec_extra)
    return record.model_copy(update={"detail": {"workspace": {"endpoint": "devai-sandbox-ws-sb-1.devai:8100"}}})


def test_the_editor_is_reachable_only_through_the_sandboxs_own_route(monkeypatch: Any) -> None:
    client = _api(_ready(ide=True), monkeypatch)

    assert client.get("/api/sandboxes/sb-1/ide/").status_code == 200
    assert client.get("/api/sandboxes/sb-other/ide/").status_code == 404


def test_a_sandbox_without_takeover_has_no_editor_to_open(monkeypatch: Any) -> None:
    client = _api(_ready(), monkeypatch)
    assert client.get("/api/sandboxes/sb-1/ide/").status_code == 409


def _socket_api(record: SandboxRecord, monkeypatch: Any) -> Any:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from devai.sandbox import routes

    async def fake_socket(websocket: Any, endpoint: str, path: str) -> None:
        await websocket.accept()
        await websocket.send_text(f"ide:{endpoint}:{path}")
        await websocket.close()

    monkeypatch.setattr(routes, "proxy_ide_socket", fake_socket)
    app = FastAPI()
    app.include_router(routes.router)
    app.state.sandbox_service = _StubService(record)
    app.state.config = None
    return TestClient(app)


def test_another_site_cannot_open_the_editors_socket(monkeypatch: Any) -> None:
    """Browsers send session cookies on a cross-origin socket handshake and no
    CORS preflight guards it, so the editor's own origin is the only one that
    may open it — otherwise any page the user visits drives their workspace."""
    from starlette.websockets import WebSocketDisconnect

    client = _socket_api(_ready(ide=True), monkeypatch)

    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect("/api/sandboxes/sb-1/ide/socket", headers={"Origin": "https://evil.example"}) as ws,
    ):
        ws.receive_text()


def test_the_dashboard_can_still_open_the_editors_socket(monkeypatch: Any) -> None:
    client = _socket_api(_ready(ide=True), monkeypatch)

    with client.websocket_connect("/api/sandboxes/sb-1/ide/socket", headers={"Origin": "http://testserver"}) as ws:
        assert ws.receive_text().startswith("ide:")
