"""What survives a sandbox (#206).

A failing eval case is only debuggable while the workspace exists, which is a
few minutes. The snapshot is what makes it debuggable a day later: the diff the
agent produced and the shape of the tree it left behind.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from devai.sandbox.models import SandboxRecord, SandboxSpec, SandboxStatus
from devai.sandbox.snapshot import capture


class _StubWorkspace:
    def __init__(self, *, shell_fails: bool = False, list_fails: bool = False) -> None:
        self.shell_fails = shell_fails
        self.list_fails = list_fails
        self.commands: list[str] = []

    async def exec(self, command: str, *, timeout: float = 120.0) -> dict[str, Any]:
        self.commands.append(command)
        if self.shell_fails:
            raise RuntimeError("workspace is gone")
        return {"exit_code": 0, "stdout": f"out:{command}", "stderr": ""}

    async def list(self, path: str = ".") -> list[str]:
        if self.list_fails:
            raise RuntimeError("workspace is gone")
        return ["app/", "app/main.py"]


def _record() -> SandboxRecord:
    now = datetime.now(UTC)
    return SandboxRecord(
        id="sb-1",
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


@pytest.mark.asyncio
async def test_a_snapshot_keeps_the_diff_the_agent_produced() -> None:
    workspace = _StubWorkspace()
    snap = await capture(workspace)

    assert "git diff" in " ".join(workspace.commands)
    assert snap["diff"].startswith("out:")


@pytest.mark.asyncio
async def test_a_snapshot_keeps_the_shape_of_the_tree_it_left() -> None:
    snap = await capture(_StubWorkspace())
    assert snap["entries"] == ["app/", "app/main.py"]


@pytest.mark.asyncio
async def test_a_snapshot_says_when_it_was_taken() -> None:
    snap = await capture(_StubWorkspace())
    assert datetime.fromisoformat(snap["captured_at"]).tzinfo is not None


@pytest.mark.asyncio
async def test_one_failed_probe_does_not_lose_the_rest_of_the_snapshot() -> None:
    snap = await capture(_StubWorkspace(shell_fails=True))

    assert snap["entries"] == ["app/", "app/main.py"]
    assert "workspace is gone" in snap["errors"]["diff"]


@pytest.mark.asyncio
async def test_a_workspace_already_gone_yields_an_empty_snapshot_not_an_error() -> None:
    snap = await capture(_StubWorkspace(shell_fails=True, list_fails=True))

    assert snap["entries"] == []
    assert snap["diff"] == ""
    assert set(snap["errors"]) == {"diff", "status", "entries"}


# ── teardown ──────────────────────────────────────────────────────────


class _Runtime:
    def __init__(self) -> None:
        self.config = type("C", (), {"namespace": "devai"})()
        self.deleted: list[tuple[str, str]] = []

    async def connect(self) -> None:
        return None

    async def apply_manifest(self, manifest: dict[str, Any]) -> None:
        return None

    async def delete_manifest(self, kind: str, name: str, namespace: str) -> None:
        self.deleted.append((kind, name))

    async def read_secret_key(self, name: str, key: str) -> str:
        return "ws-token"


class _Store:
    def __init__(self) -> None:
        self.statuses: list[tuple[str, dict[str, Any] | None]] = []

    async def set_sandbox_status(self, sandbox_id: str, status: str, detail: dict[str, Any] | None = None) -> None:
        self.statuses.append((status, detail))


def _provisioner(monkeypatch: pytest.MonkeyPatch, workspace: _StubWorkspace) -> tuple[Any, _Store]:
    from devai.sandbox import provisioner as prov

    monkeypatch.setattr(prov, "WorkspaceClient", lambda *a, **k: workspace)
    store = _Store()
    return prov.SandboxProvisioner(_Runtime(), store), store


@pytest.mark.asyncio
async def test_teardown_snapshots_the_workspace_before_deleting_it(monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = _StubWorkspace()
    provisioner, store = _provisioner(monkeypatch, workspace)

    await provisioner.teardown(_record())

    destroyed = [detail for status, detail in store.statuses if status == SandboxStatus.DESTROYED.value]
    assert destroyed[0]["snapshot"]["entries"] == ["app/", "app/main.py"]


@pytest.mark.asyncio
async def test_a_snapshot_that_fails_does_not_leave_the_sandbox_standing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _StubWorkspace(shell_fails=True, list_fails=True)
    provisioner, store = _provisioner(monkeypatch, workspace)

    record = await provisioner.teardown(_record())

    assert record.status is SandboxStatus.DESTROYED
    assert [status for status, _ in store.statuses][-1] == SandboxStatus.DESTROYED.value
