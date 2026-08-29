"""The sandbox workspace — a place to work, not just a boundary (#201).

Two properties are load-bearing here. The workspace is confined to /workspace,
because a shell and a file API that can read the pod's whole filesystem is not a
sandbox. And every route needs the capability token: unlike the prior art we
took this from, there is no unauthenticated mode to forget to turn off.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from devai.sandbox.models import AgentRef, ModelRef, SandboxRecord, SandboxSpec, SandboxStatus
from devai.sandbox.workspace import BROWSER_PORT, WorkspaceError, WorkspaceFiles, build_workspace_manifests


def _record(sandbox_id: str = "sb-1") -> SandboxRecord:
    now = datetime.now(UTC)
    return SandboxRecord(
        id=sandbox_id,
        owner="dev@example.com",
        spec=SandboxSpec(
            agent=AgentRef(name="reviewer", version="1"),
            model=ModelRef(provider="anthropic", model="claude-sonnet-5"),
        ),
        status=SandboxStatus.PENDING,
        created_at=now,
        expires_at=now + timedelta(hours=1),
    )


# ── confinement ───────────────────────────────────────────────────────


@pytest.fixture
def files(tmp_path: Path) -> WorkspaceFiles:
    return WorkspaceFiles(root=tmp_path)


def test_a_file_is_written_and_read_back(files: WorkspaceFiles) -> None:
    files.write("notes/plan.md", "# plan")

    assert files.read("notes/plan.md") == "# plan"


def test_reading_outside_the_workspace_is_refused(files: WorkspaceFiles) -> None:
    with pytest.raises(WorkspaceError):
        files.read("../../etc/passwd")


def test_writing_outside_the_workspace_is_refused(files: WorkspaceFiles) -> None:
    with pytest.raises(WorkspaceError):
        files.write("/etc/cron.d/pwn", "* * * * * root sh")


def test_an_absolute_path_inside_the_workspace_is_fine(files: WorkspaceFiles, tmp_path: Path) -> None:
    files.write(str(tmp_path / "a.txt"), "ok")

    assert files.read("a.txt") == "ok"


def test_a_symlink_out_of_the_workspace_does_not_widen_it(files: WorkspaceFiles, tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret")
    (tmp_path / "link.txt").symlink_to(outside)

    with pytest.raises(WorkspaceError):
        files.read("link.txt")


def test_listing_shows_workspace_relative_paths(files: WorkspaceFiles) -> None:
    files.write("a.txt", "1")
    files.write("sub/b.txt", "2")

    assert sorted(files.list(".")) == ["a.txt", "sub"]


def test_search_returns_matching_lines_with_their_file_and_line_number(files: WorkspaceFiles) -> None:
    files.write("a.py", "import os\nDEBUG = True\n")

    hits = files.search("DEBUG")

    assert hits == [{"path": "a.py", "line": 2, "text": "DEBUG = True"}]


def test_replace_edits_in_place(files: WorkspaceFiles) -> None:
    files.write("a.py", "DEBUG = True")

    files.replace("a.py", "True", "False")

    assert files.read("a.py") == "DEBUG = False"


def test_reading_a_missing_file_is_an_error_not_an_empty_string(files: WorkspaceFiles) -> None:
    with pytest.raises(WorkspaceError):
        files.read("nope.txt")


# ── manifests ─────────────────────────────────────────────────────────


def _by_kind(manifests: list[dict[str, Any]], kind: str) -> dict[str, Any]:
    return next(m for m in manifests if m["kind"] == kind)


def test_a_workspace_is_a_pvc_a_secret_a_pod_and_a_service() -> None:
    kinds = [m["kind"] for m in build_workspace_manifests(_record(), namespace="devai", token="t0k3n")]

    assert kinds == ["PersistentVolumeClaim", "Secret", "Pod", "Service"]


def test_the_workspace_volume_is_mounted_at_workspace() -> None:
    pod = _by_kind(build_workspace_manifests(_record(), namespace="devai", token="t"), "Pod")
    container = pod["spec"]["containers"][0]

    mount = next(m for m in container["volumeMounts"] if m["name"] == "workspace")
    assert mount["mountPath"] == "/workspace"


def test_the_workspace_carries_the_sandbox_label_so_the_isolation_objects_select_it() -> None:
    from devai.sandbox.job import SANDBOX_LABEL

    pod = _by_kind(build_workspace_manifests(_record("sb-9"), namespace="devai", token="t"), "Pod")

    assert pod["metadata"]["labels"][SANDBOX_LABEL] == "sb-9"


def test_the_workspace_pod_runs_unprivileged_with_no_service_account_token() -> None:
    pod = _by_kind(build_workspace_manifests(_record(), namespace="devai", token="t"), "Pod")

    assert pod["spec"]["automountServiceAccountToken"] is False
    assert pod["spec"]["securityContext"]["runAsNonRoot"] is True
    sc = pod["spec"]["containers"][0]["securityContext"]
    assert sc["allowPrivilegeEscalation"] is False
    assert sc["capabilities"]["drop"] == ["ALL"]


def test_a_browser_workspace_uses_the_browser_image_and_proxy() -> None:
    record = _record().model_copy(
        update={"spec": _record().spec.model_copy(update={"workspace": True, "browser": True})}
    )
    pod = _by_kind(build_workspace_manifests(record, namespace="devai", token="tok"), "Pod")
    container = pod["spec"]["containers"][0]
    env = {entry["name"]: entry.get("value") for entry in container["env"]}

    assert container["image"] == "ghcr.io/tesserix/devai/devai-browser:latest"
    assert env["DEVAI_WORKSPACE_BROWSER"] == "true"
    assert env["HTTPS_PROXY"] == "http://devai-sandbox-proxy-sb-1.devai.svc.cluster.local:8118"
    assert pod["spec"]["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    assert "unconfined" not in str(pod).lower()


def test_a_browser_workspace_does_not_expose_novnc_on_its_cluster_service() -> None:
    record = _record().model_copy(
        update={"spec": _record().spec.model_copy(update={"workspace": True, "browser": True})}
    )
    service = _by_kind(build_workspace_manifests(record, namespace="devai", token="tok"), "Service")

    assert BROWSER_PORT not in {port["port"] for port in service["spec"]["ports"]}


def test_the_capability_token_reaches_the_pod_only_through_a_secret() -> None:
    manifests = build_workspace_manifests(_record(), namespace="devai", token="t0k3n")
    pod = _by_kind(manifests, "Pod")
    secret = _by_kind(manifests, "Secret")

    assert secret["stringData"]["token"] == "t0k3n"
    env = pod["spec"]["containers"][0]["env"]
    token_env = next(e for e in env if e["name"] == "DEVAI_WORKSPACE_TOKEN")
    assert token_env["valueFrom"]["secretKeyRef"]["name"] == secret["metadata"]["name"]
    assert "t0k3n" not in str(pod)


def test_the_service_is_cluster_internal_only() -> None:
    svc = _by_kind(build_workspace_manifests(_record(), namespace="devai", token="t"), "Service")

    assert svc["spec"]["type"] == "ClusterIP"


# ── the HTTP surface: no unauthenticated mode exists ──────────────────


@pytest.fixture
def client(tmp_path: Path, monkeypatch: Any) -> Any:
    from fastapi.testclient import TestClient

    from devai.sandbox.workspace_server import create_workspace_app

    return TestClient(create_workspace_app(root=tmp_path, token="t0k3n"))


def _auth() -> dict[str, str]:
    return {"X-DevAI-Workspace-Token": "t0k3n"}


def test_a_call_without_the_token_is_rejected(client: Any) -> None:
    assert client.post("/file/read", json={"path": "a.txt"}).status_code == 401


def test_a_call_with_the_wrong_token_is_rejected(client: Any) -> None:
    r = client.post("/file/read", json={"path": "a.txt"}, headers={"X-DevAI-Workspace-Token": "guess"})

    assert r.status_code == 401


def test_a_server_started_without_a_token_refuses_to_run(tmp_path: Path) -> None:
    from devai.sandbox.workspace_server import create_workspace_app

    with pytest.raises(ValueError, match="token"):
        create_workspace_app(root=tmp_path, token="")


def test_write_then_read_over_http(client: Any) -> None:
    client.post("/file/write", json={"path": "a.txt", "content": "hello"}, headers=_auth())

    r = client.post("/file/read", json={"path": "a.txt"}, headers=_auth())

    assert r.status_code == 200
    assert r.json()["content"] == "hello"


def test_a_path_escape_over_http_is_a_400_not_a_traceback(client: Any) -> None:
    r = client.post("/file/read", json={"path": "../../etc/passwd"}, headers=_auth())

    assert r.status_code == 400
    assert "outside the workspace" in r.json()["detail"]


def test_shell_exec_runs_in_the_workspace(client: Any) -> None:
    client.post("/file/write", json={"path": "a.txt", "content": "hi"}, headers=_auth())

    r = client.post("/shell/exec", json={"command": "ls"}, headers=_auth())

    assert r.json()["exit_code"] == 0
    assert "a.txt" in r.json()["stdout"]


def test_a_failing_command_reports_its_exit_code_rather_than_erroring(client: Any) -> None:
    r = client.post("/shell/exec", json={"command": "exit 3"}, headers=_auth())

    assert r.status_code == 200
    assert r.json()["exit_code"] == 3


def test_healthz_needs_no_token_so_kubelet_can_probe_it(client: Any) -> None:
    assert client.get("/healthz").status_code == 200


# ── provisioning ──────────────────────────────────────────────────────


class _FakeRuntime:
    def __init__(self) -> None:
        self.applied: list[dict[str, Any]] = []
        self.deleted: list[tuple[str, str]] = []
        self.config = type("C", (), {"namespace": "devai"})()

    async def connect(self) -> None:
        return None

    async def apply_manifest(self, manifest: dict[str, Any]) -> None:
        self.applied.append(manifest)

    async def delete_manifest(self, kind: str, name: str, namespace: str | None = None) -> None:
        self.deleted.append((kind, name))


class _FakeStore:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    async def set_sandbox_status(self, sandbox_id: str, status: str, detail: dict[str, Any] | None = None) -> None:
        self.calls.append((sandbox_id, status, detail))


def _with_workspace(sandbox_id: str = "sb-1") -> SandboxRecord:
    rec = _record(sandbox_id)
    return rec.model_copy(update={"spec": rec.spec.model_copy(update={"workspace": True})})


@pytest.mark.asyncio
async def test_a_sandbox_without_a_workspace_gets_no_volume_and_no_workspace_pod() -> None:
    from devai.sandbox.provisioner import SandboxProvisioner

    rt = _FakeRuntime()
    await SandboxProvisioner(rt, _FakeStore()).provision(_record())

    # The egress proxy pod is provisioned for every sandbox; the workspace is not.
    assert not [m for m in rt.applied if m["kind"] == "PersistentVolumeClaim"]
    assert not [m for m in rt.applied if m["metadata"]["name"].startswith("devai-sandbox-ws-")]


@pytest.mark.asyncio
async def test_asking_for_a_workspace_provisions_the_volume_pod_and_service() -> None:
    from devai.sandbox.provisioner import SandboxProvisioner

    rt = _FakeRuntime()
    await SandboxProvisioner(rt, _FakeStore()).provision(_with_workspace())

    kinds = [m["kind"] for m in rt.applied]
    assert kinds[-4:] == ["PersistentVolumeClaim", "Secret", "Pod", "Service"]


@pytest.mark.asyncio
async def test_the_workspace_endpoint_is_recorded_so_the_api_can_reach_it() -> None:
    from devai.sandbox.provisioner import SandboxProvisioner

    store = _FakeStore()
    result = await SandboxProvisioner(_FakeRuntime(), store).provision(_with_workspace("sb-7"))

    assert "devai-sandbox-ws-sb-7" in result.detail["workspace"]["endpoint"]


@pytest.mark.asyncio
async def test_the_capability_token_is_never_written_to_the_sandbox_record() -> None:
    from devai.sandbox.provisioner import SandboxProvisioner

    rt = _FakeRuntime()
    store = _FakeStore()
    result = await SandboxProvisioner(rt, store).provision(_with_workspace())

    ws = next(m for m in rt.applied if m["metadata"]["name"].startswith("devai-sandbox-ws-") and m["kind"] == "Secret")
    token = ws["stringData"]["token"]
    assert token not in str(result.detail)
    assert token not in str(store.calls)


@pytest.mark.asyncio
async def test_teardown_removes_the_workspace_with_the_sandbox() -> None:
    from devai.sandbox.provisioner import SandboxProvisioner

    rt = _FakeRuntime()
    await SandboxProvisioner(rt, _FakeStore()).teardown(_with_workspace())

    assert {"Pod", "Service", "PersistentVolumeClaim", "Secret"} <= {k for k, _ in rt.deleted}


# ── reaching a workspace from the API ─────────────────────────────────


@pytest.mark.asyncio
async def test_the_client_sends_the_capability_token(monkeypatch: Any) -> None:
    from devai.sandbox.workspace_client import WorkspaceClient

    seen: dict[str, Any] = {}

    class _Resp:
        status_code = 200

        def json(self) -> dict[str, Any]:
            return {"content": "hi"}

        def raise_for_status(self) -> None:
            return None

    class _HTTP:
        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *a: Any) -> None:
            return None

        async def post(self, url: str, json: dict[str, Any], headers: dict[str, str]) -> Any:
            seen.update({"url": url, "json": json, "headers": headers})
            return _Resp()

    monkeypatch.setattr("devai.sandbox.workspace_client.httpx.AsyncClient", lambda **kw: _HTTP())

    out = await WorkspaceClient("ws-host:8100", token="t0k3n").read("a.txt")

    assert out == "hi"
    assert seen["headers"]["X-DevAI-Workspace-Token"] == "t0k3n"
    assert seen["url"].endswith("/file/read")


@pytest.mark.asyncio
async def test_a_workspace_call_on_a_sandbox_that_has_none_is_a_409(monkeypatch: Any) -> None:
    from fastapi import HTTPException

    from devai.sandbox.routes import _workspace_client

    class _Req:
        app = type("A", (), {"state": type("S", (), {})()})()

    with pytest.raises(HTTPException) as e:
        await _workspace_client(_Req(), _record())  # type: ignore[arg-type]

    assert e.value.status_code == 409
