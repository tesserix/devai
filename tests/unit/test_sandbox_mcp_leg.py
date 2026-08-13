"""The sandbox workspace reaches agents as MCP tools, or not at all."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from devai.mcphub.model import SEP
from devai.mcphub.sandbox_leg import SANDBOX_SERVER, WorkspaceLeg
from devai.sandbox.gateway import ToolGateway, is_side_effecting
from devai.sandbox.models import ToolMode

_NOW = datetime(2026, 8, 13, tzinfo=UTC)


class _StubWorkspace:
    """Stands in for the workspace HTTP surface."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._fail = fail

    def _record(self, op: str, **kwargs: Any) -> Any:
        self.calls.append((op, kwargs))
        if self._fail:
            raise RuntimeError("workspace is gone")
        return {"op": op, **kwargs}

    async def exec(self, command: str, *, timeout: float = 120.0) -> dict[str, Any]:
        return self._record("exec", command=command, timeout=timeout)

    async def read(self, path: str) -> str:
        self._record("read", path=path)
        return "file body"

    async def write(self, path: str, content: str) -> dict[str, Any]:
        return self._record("write", path=path, content=content)

    async def list(self, path: str = ".") -> list[str]:
        self._record("list", path=path)
        return ["a.py"]

    async def search(self, needle: str, path: str = ".") -> list[dict[str, Any]]:
        self._record("search", needle=needle, path=path)
        return [{"path": "a.py", "line": 1}]


def _leg(workspace: _StubWorkspace | None = None) -> WorkspaceLeg:
    return WorkspaceLeg(client=workspace or _StubWorkspace())


def test_outside_a_sandbox_there_is_no_workspace_leg() -> None:
    assert WorkspaceLeg.from_env({}) is None
    assert WorkspaceLeg.from_env({"DEVAI_SANDBOX_ID": "sb-1"}) is None


def test_a_sandbox_with_a_workspace_gets_a_leg() -> None:
    leg = WorkspaceLeg.from_env(
        {
            "DEVAI_SANDBOX_ID": "sb-1",
            "DEVAI_SANDBOX_WORKSPACE": "devai-sandbox-ws-sb-1.devai.svc.cluster.local:8100",
            "DEVAI_SANDBOX_WORKSPACE_TOKEN": "tok",
        }
    )
    assert leg is not None


def test_a_workspace_without_its_token_is_not_federated() -> None:
    leg = WorkspaceLeg.from_env({"DEVAI_SANDBOX_ID": "sb-1", "DEVAI_SANDBOX_WORKSPACE": "ws:8100"})
    assert leg is None


def test_every_workspace_capability_is_a_namespaced_tool() -> None:
    names = {t.name for t in _leg().tools()}
    assert names == {
        f"{SANDBOX_SERVER}{SEP}shell_exec",
        f"{SANDBOX_SERVER}{SEP}file_read",
        f"{SANDBOX_SERVER}{SEP}file_write",
        f"{SANDBOX_SERVER}{SEP}file_list",
        f"{SANDBOX_SERVER}{SEP}file_search",
    }


def test_each_tool_declares_the_arguments_it_takes() -> None:
    schemas = {t.wire_name: t.input_schema for t in _leg().tools()}
    assert schemas["shell_exec"]["properties"]["command"]["type"] == "string"
    assert schemas["shell_exec"]["required"] == ["command"]
    assert schemas["file_write"]["required"] == ["path", "content"]


def test_workspace_tools_carry_the_sandbox_label_for_budgeting() -> None:
    for tool in _leg().tools():
        assert tool.labels["mcp.devai.io/server"] == SANDBOX_SERVER
        assert tool.tier == "core"


def test_the_leg_owns_only_its_own_namespace() -> None:
    leg = _leg()
    assert leg.owns(f"{SANDBOX_SERVER}{SEP}shell_exec")
    assert not leg.owns("analyst__security_scan_sast")
    assert not leg.owns("shell_exec")


@pytest.mark.asyncio
async def test_a_call_reaches_the_workspace_it_was_issued_for() -> None:
    workspace = _StubWorkspace()
    result = await _leg(workspace).call(f"{SANDBOX_SERVER}{SEP}shell_exec", {"command": "pytest -q"})
    assert workspace.calls == [("exec", {"command": "pytest -q", "timeout": 120.0})]
    assert result["op"] == "exec"


@pytest.mark.asyncio
async def test_file_operations_route_to_their_own_workspace_verb() -> None:
    workspace = _StubWorkspace()
    leg = _leg(workspace)
    await leg.call(f"{SANDBOX_SERVER}{SEP}file_read", {"path": "a.py"})
    await leg.call(f"{SANDBOX_SERVER}{SEP}file_write", {"path": "a.py", "content": "x"})
    await leg.call(f"{SANDBOX_SERVER}{SEP}file_list", {"path": "."})
    await leg.call(f"{SANDBOX_SERVER}{SEP}file_search", {"needle": "TODO"})
    assert [op for op, _ in workspace.calls] == ["read", "write", "list", "search"]


@pytest.mark.asyncio
async def test_an_unknown_workspace_tool_is_refused_rather_than_guessed() -> None:
    with pytest.raises(ValueError, match="rm_rf"):
        await _leg().call(f"{SANDBOX_SERVER}{SEP}rm_rf", {})


@pytest.mark.asyncio
async def test_a_reaped_workspace_drops_out_of_the_aggregate() -> None:
    leg = _leg(_StubWorkspace(fail=True))
    assert await leg.probe() is False
    assert leg.tools() == []


@pytest.mark.asyncio
async def test_a_live_workspace_stays_in_the_aggregate() -> None:
    leg = _leg()
    assert await leg.probe() is True
    assert len(leg.tools()) == 5


def test_workspace_writes_are_side_effecting_and_reads_are_not() -> None:
    assert is_side_effecting(f"{SANDBOX_SERVER}{SEP}shell_exec")
    assert is_side_effecting(f"{SANDBOX_SERVER}{SEP}file_write")
    assert not is_side_effecting(f"{SANDBOX_SERVER}{SEP}file_read")
    assert not is_side_effecting(f"{SANDBOX_SERVER}{SEP}file_list")
    assert not is_side_effecting(f"{SANDBOX_SERVER}{SEP}file_search")


def _job_env(*, workspace: bool) -> dict[str, Any]:
    from devai.sandbox.job import apply_sandbox_boundary
    from devai.sandbox.models import SandboxRecord, SandboxSpec, SandboxStatus

    record = SandboxRecord(
        id="sb-1",
        owner="alice@x.com",
        spec=SandboxSpec.model_validate(
            {
                "agent": {"name": "dev", "version": "1"},
                "model": {"provider": "anthropic", "model": "claude-sonnet-5"},
                "workspace": workspace,
            }
        ),
        status=SandboxStatus.READY,
        created_at=_NOW,
        expires_at=_NOW + timedelta(hours=4),
    )
    job = {
        "metadata": {"name": "j"},
        "spec": {"template": {"metadata": {}, "spec": {"containers": [{"name": "c", "env": []}]}}},
    }
    fenced = apply_sandbox_boundary(job, record)
    return {e["name"]: e for e in fenced["spec"]["template"]["spec"]["containers"][0]["env"]}


def test_a_sandbox_pod_is_told_where_its_workspace_is() -> None:
    env = _job_env(workspace=True)
    assert "devai-sandbox-ws-sb-1" in env["DEVAI_SANDBOX_WORKSPACE"]["value"]
    ref = env["DEVAI_SANDBOX_WORKSPACE_TOKEN"]["valueFrom"]["secretKeyRef"]
    assert (ref["name"], ref["key"]) == ("devai-sandbox-ws-sb-1", "token")


def test_a_sandbox_without_a_workspace_is_told_nothing_to_connect_to() -> None:
    env = _job_env(workspace=False)
    assert "DEVAI_SANDBOX_WORKSPACE" not in env
    assert "DEVAI_SANDBOX_WORKSPACE_TOKEN" not in env


class _Registry:
    def list_mcp_servers(self) -> list[dict[str, Any]]:
        return []

    def list_tool_artifacts(self) -> list[dict[str, Any]]:
        return []


def _hub(workspace: _StubWorkspace | None = None) -> Any:
    from devai.mcphub.hub import MCPHub

    hub = MCPHub(_Registry())
    hub.sandbox = _leg(workspace)
    return hub


def test_a_hub_outside_a_sandbox_federates_no_workspace_tools() -> None:
    from devai.mcphub.hub import MCPHub

    hub = MCPHub(_Registry())
    assert hub.sandbox is None
    assert hub.list_tools().selected == []


def test_the_hub_presents_the_workspace_alongside_registry_tools() -> None:
    names = {t.name for t in _hub().list_tools().selected}
    assert f"{SANDBOX_SERVER}{SEP}shell_exec" in names


@pytest.mark.asyncio
async def test_the_hub_routes_a_workspace_call_to_the_sandbox_it_runs_in() -> None:
    workspace = _StubWorkspace()
    hub = _hub(workspace)
    await hub.call_tool(f"{SANDBOX_SERVER}{SEP}file_read", {"path": "a.py"})
    assert workspace.calls == [("read", {"path": "a.py"})]


@pytest.mark.asyncio
async def test_the_same_call_from_outside_the_sandbox_resolves_nothing() -> None:
    from devai.mcphub.downstream import DownstreamError
    from devai.mcphub.hub import MCPHub

    hub = MCPHub(_Registry())
    with pytest.raises(DownstreamError):
        await hub.call_tool(f"{SANDBOX_SERVER}{SEP}file_read", {"path": "a.py"})


@pytest.mark.asyncio
async def test_a_workspace_call_is_recorded_under_the_mode_it_ran_in() -> None:
    workspace = _StubWorkspace()
    hub = _hub(workspace)
    hub._gateway = ToolGateway.from_env({"DEVAI_SANDBOX_TOOL_MODE": "real"})
    await hub.call_tool(f"{SANDBOX_SERVER}{SEP}shell_exec", {"command": "rm -rf /"})
    record = hub._gateway.records[-1]
    assert record.blocked is True
    assert record.mode is ToolMode.BLOCK
    assert workspace.calls == []


def test_a_workspace_write_needs_an_explicit_override_to_run_for_real() -> None:
    gateway = ToolGateway.from_env({"DEVAI_SANDBOX_TOOL_MODE": "real"})
    assert gateway is not None
    assert gateway.mode_for(f"{SANDBOX_SERVER}{SEP}shell_exec") is ToolMode.BLOCK
    assert gateway.mode_for(f"{SANDBOX_SERVER}{SEP}file_read") is ToolMode.REAL

    allowed = ToolGateway.from_env(
        {
            "DEVAI_SANDBOX_TOOL_MODE": "real",
            "DEVAI_SANDBOX_TOOL_OVERRIDES": json.dumps({f"{SANDBOX_SERVER}{SEP}shell_exec": "real"}),
        }
    )
    assert allowed is not None
    assert allowed.mode_for(f"{SANDBOX_SERVER}{SEP}shell_exec") is ToolMode.REAL
