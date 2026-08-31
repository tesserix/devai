"""The egress proxy is the only route out of a sandbox (#204).

The tests that matter here are the denials: an allowlist nobody has watched
refuse anything is indistinguishable from allow-all.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from devai.sandbox.egress import (
    DEFAULT_ALLOWLIST,
    PROXY_PORT,
    build_proxy_manifests,
    host_allowed,
    proxy_env,
    proxy_service_host,
)
from devai.sandbox.egress_proxy import serve_proxy
from devai.sandbox.models import AgentRef, ModelRef, SandboxRecord, SandboxSpec, SandboxStatus


def _record(**spec_kwargs) -> SandboxRecord:
    now = datetime.now(UTC)
    spec = SandboxSpec(
        agent=AgentRef(name="reviewer", version="1"),
        model=ModelRef(provider="anthropic", model="claude-sonnet-5"),
        **spec_kwargs,
    )
    return SandboxRecord(
        id="sb1234",
        owner="dev@example.com",
        spec=spec,
        status=SandboxStatus.PENDING,
        created_at=now,
        expires_at=now + timedelta(hours=1),
    )


# ── allowlist matching ────────────────────────────────────────────────


def test_an_exact_host_on_the_list_is_allowed():
    assert host_allowed("pypi.org", ["pypi.org"])


def test_a_host_that_is_not_on_the_list_is_refused():
    assert not host_allowed("evil.example.com", ["pypi.org"])


def test_a_leading_dot_pattern_covers_subdomains_and_the_apex():
    assert host_allowed("files.pythonhosted.org", [".pythonhosted.org"])
    assert host_allowed("pythonhosted.org", [".pythonhosted.org"])


def test_a_suffix_that_is_not_a_domain_boundary_is_refused():
    # notgithub.com must not match github.com by plain string suffix.
    assert not host_allowed("notgithub.com", ["github.com"])


def test_the_port_and_case_are_ignored_when_matching():
    assert host_allowed("GitHub.com:443", ["github.com"])


def test_an_empty_allowlist_allows_nothing():
    assert not host_allowed("pypi.org", [])


def test_the_default_allowlist_carries_the_registries_real_work_needs():
    assert host_allowed("pypi.org", DEFAULT_ALLOWLIST)
    assert host_allowed("registry.npmjs.org", DEFAULT_ALLOWLIST)
    assert host_allowed("github.com", DEFAULT_ALLOWLIST)
    assert not host_allowed("pastebin.com", DEFAULT_ALLOWLIST)


# ── the proxy itself ──────────────────────────────────────────────────


async def _proxy(allowlist: list[str]) -> tuple[int, list[dict], asyncio.AbstractServer]:
    log: list[dict] = []
    server = await serve_proxy(allowlist, host="127.0.0.1", port=0, on_record=log.append)
    port = server.sockets[0].getsockname()[1]
    return port, log, server


async def _connect(port: int, target: str) -> str:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
    await writer.drain()
    status = (await reader.readline()).decode()
    writer.close()
    return status


@pytest.mark.asyncio
async def test_a_disallowed_connect_is_refused_with_a_legible_403():
    port, _log, server = await _proxy(["pypi.org"])
    try:
        status = await _connect(port, "evil.example.com:443")
    finally:
        server.close()
    assert "403" in status


@pytest.mark.asyncio
async def test_a_refusal_names_the_host_so_the_agent_can_report_it():
    port, _log, server = await _proxy(["pypi.org"])
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"CONNECT evil.example.com:443 HTTP/1.1\r\n\r\n")
        await writer.drain()
        body = (await reader.read(4096)).decode()
        writer.close()
    finally:
        server.close()
    assert "evil.example.com" in body


@pytest.mark.asyncio
async def test_an_allowed_host_is_tunnelled_through_to_the_origin():
    async def origin(reader, writer):
        writer.write(b"hello from origin")
        await writer.drain()
        writer.close()

    upstream = await asyncio.start_server(origin, "127.0.0.1", 0)
    upstream_port = upstream.sockets[0].getsockname()[1]
    port, _log, server = await _proxy(["127.0.0.1"])
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(f"CONNECT 127.0.0.1:{upstream_port} HTTP/1.1\r\n\r\n".encode())
        await writer.drain()
        status = (await reader.readline()).decode()
        assert "200" in status
        await reader.readuntil(b"\r\n")  # end of the proxy's response headers
        payload = await reader.read(64)
        writer.close()
    finally:
        server.close()
        upstream.close()
    assert b"hello from origin" in payload


@pytest.mark.asyncio
async def test_every_attempt_is_recorded_whether_allowed_or_denied():
    port, log, server = await _proxy(["pypi.org"])
    try:
        await _connect(port, "evil.example.com:443")
    finally:
        server.close()
    assert log and log[0]["host"] == "evil.example.com"
    assert log[0]["allowed"] is False


@pytest.mark.asyncio
async def test_a_plain_http_request_to_a_disallowed_host_is_refused():
    port, _log, server = await _proxy(["pypi.org"])
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET http://evil.example.com/x HTTP/1.1\r\nHost: evil.example.com\r\n\r\n")
        await writer.drain()
        status = (await reader.readline()).decode()
        writer.close()
    finally:
        server.close()
    assert "403" in status


@pytest.mark.asyncio
async def test_a_malformed_request_is_refused_rather_than_crashing_the_proxy():
    port, _log, server = await _proxy(["pypi.org"])
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"garbage\r\n\r\n")
        await writer.drain()
        status = (await reader.readline()).decode()
        writer.close()
        # still serving
        assert "400" in status
        assert "403" in await _connect(port, "evil.example.com:443")
    finally:
        server.close()


# ── wiring: env, manifests, spec ──────────────────────────────────────


def test_the_proxy_env_points_at_this_sandboxs_own_proxy():
    env = {e["name"]: e["value"] for e in proxy_env("sb1234", namespace="devai")}
    assert env["HTTPS_PROXY"] == f"http://{proxy_service_host('sb1234', namespace='devai')}"
    assert env["HTTPS_PROXY"] == env["https_proxy"]
    assert str(PROXY_PORT) in env["HTTP_PROXY"]


def test_cluster_local_traffic_bypasses_the_proxy():
    env = {e["name"]: e["value"] for e in proxy_env("sb1234", namespace="devai")}
    assert ".svc.cluster.local" in env["NO_PROXY"]
    assert "localhost" in env["NO_PROXY"]


def test_a_sandbox_may_add_domains_and_they_reach_the_proxy_config():
    record = _record(allow_domains=["internal.example.com"])
    configmap = build_proxy_manifests(record, namespace="devai")[0]
    allowlist = configmap["data"]["allowlist"].split()
    assert "internal.example.com" in allowlist
    assert "pypi.org" in allowlist


def test_the_default_spec_adds_no_domains():
    assert _record().spec.allow_domains == []


def test_the_proxy_manifests_are_configmap_pod_service_in_that_order():
    kinds = [m["kind"] for m in build_proxy_manifests(_record(), namespace="devai")]
    assert kinds == ["ConfigMap", "Pod", "Service"]


def test_the_proxy_pod_is_labelled_with_its_sandbox():
    from devai.sandbox.job import SANDBOX_LABEL

    pod = build_proxy_manifests(_record(), namespace="devai")[1]
    assert pod["metadata"]["labels"][SANDBOX_LABEL] == "sb1234"


def test_the_proxy_pod_is_unprivileged_and_carries_no_service_account_token():
    pod = build_proxy_manifests(_record(), namespace="devai")[1]
    assert pod["spec"]["automountServiceAccountToken"] is False
    assert pod["spec"]["securityContext"]["runAsNonRoot"] is True
    assert pod["spec"]["containers"][0]["securityContext"]["capabilities"]["drop"] == ["ALL"]


def test_the_proxy_pod_passes_the_restricted_pod_security_profile():
    # The sandbox namespace enforces PSA restricted; without a seccompProfile
    # the API server rejects the pod and provisioning fails.
    pod = build_proxy_manifests(_record(), namespace="devai-sbx-sb1234")[1]
    assert pod["spec"]["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    assert pod["spec"]["containers"][0]["securityContext"]["allowPrivilegeEscalation"] is False


def test_the_proxy_pod_gets_no_proxy_env_of_its_own():
    # It is the way out; pointing it at itself would loop.
    pod = build_proxy_manifests(_record(), namespace="devai")[1]
    names = {e["name"] for e in pod["spec"]["containers"][0].get("env", [])}
    assert "HTTPS_PROXY" not in names


class _FakeRuntime:
    def __init__(self) -> None:
        self.applied: list[dict] = []
        self.deleted: list[tuple[str, str]] = []
        self.config = type("C", (), {"namespace": "devai"})()

    async def connect(self) -> None:
        return None

    async def apply_manifest(self, manifest: dict) -> None:
        self.applied.append(manifest)

    async def delete_manifest(self, kind: str, name: str, namespace: str | None = None) -> None:
        self.deleted.append((kind, name))


class _FakeStore:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def set_sandbox_status(self, sandbox_id: str, status: str, detail: dict | None = None) -> None:
        self.calls.append((sandbox_id, status, detail))


@pytest.mark.asyncio
async def test_every_sandbox_gets_a_proxy_because_it_is_the_only_way_out():
    from devai.sandbox.provisioner import SandboxProvisioner

    rt = _FakeRuntime()
    await SandboxProvisioner(rt, _FakeStore()).provision(_record())

    names = {m["metadata"]["name"] for m in rt.applied}
    assert "devai-sandbox-proxy-sb1234" in names


@pytest.mark.asyncio
async def test_the_proxy_endpoint_is_recorded_on_the_sandbox():
    from devai.sandbox.provisioner import SandboxProvisioner

    result = await SandboxProvisioner(_FakeRuntime(), _FakeStore()).provision(_record())

    assert result.detail["egress"]["proxy"] == proxy_service_host("sb1234", namespace="devai-sbx-sb1234")


@pytest.mark.asyncio
async def test_the_added_domains_are_recorded_so_the_run_is_auditable():
    from devai.sandbox.provisioner import SandboxProvisioner

    result = await SandboxProvisioner(_FakeRuntime(), _FakeStore()).provision(
        _record(allow_domains=["internal.example.com"])
    )

    assert result.detail["egress"]["added_domains"] == ["internal.example.com"]


@pytest.mark.asyncio
async def test_teardown_removes_the_proxy_with_the_sandbox():
    from devai.sandbox.provisioner import SandboxProvisioner

    rt = _FakeRuntime()
    await SandboxProvisioner(rt, _FakeStore()).teardown(_record())

    assert ("ConfigMap", "devai-sandbox-proxy-sb1234") in rt.deleted
    assert ("Pod", "devai-sandbox-proxy-sb1234") in rt.deleted


def test_a_sandboxed_job_is_pointed_at_the_proxy():
    from devai.sandbox.job import apply_sandbox_boundary

    job = {
        "metadata": {"name": "j"},
        "spec": {"template": {"metadata": {}, "spec": {"containers": [{"name": "runner", "env": []}]}}},
    }
    fenced = apply_sandbox_boundary(job, _record())
    env = {e["name"]: e.get("value") for e in fenced["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["HTTPS_PROXY"].endswith(str(PROXY_PORT))


def test_every_kind_a_sandbox_emits_can_be_applied() -> None:
    """A manifest a builder emits but the client cannot route fails the whole
    sandbox at creation, and the only evidence is a string in `detail`.

    Asserted against the builders rather than a hand-kept list of kinds, because
    a hand-kept list is what let ConfigMap go missing.
    """
    from devai.runtime.k8s_client import K8sJobRuntime
    from devai.sandbox.isolation import build_isolation_manifests
    from devai.sandbox.workspace import build_workspace_manifests

    rt = K8sJobRuntime.__new__(K8sJobRuntime)
    rt._core_v1 = object()  # noqa: SLF001
    rt._networking_v1 = object()  # noqa: SLF001

    record = _record()
    manifests = [
        *build_isolation_manifests(record, namespace="devai"),
        *build_proxy_manifests(record, namespace="devai"),
        *build_workspace_manifests(record, namespace="devai", token="-"),
    ]
    for manifest in manifests:
        rt._manifest_api(manifest["kind"])  # noqa: SLF001 — raises if unroutable


def test_dns_egress_allows_the_cluster_nameserver_ip(tmp_path, monkeypatch):
    """GKE Dataplane V2 matches the kube-dns ClusterIP, which a kube-system
    namespaceSelector alone does not cover — without the ipBlock every lookup
    from the sandbox times out.
    """
    from devai.sandbox import isolation
    from devai.sandbox.isolation import build_isolation_manifests

    resolv = tmp_path / "resolv.conf"
    resolv.write_text("search devai.svc.cluster.local svc.cluster.local\nnameserver 10.30.0.10\noptions ndots:5\n")
    monkeypatch.setattr(isolation, "_RESOLV_CONF", str(resolv))

    policy = build_isolation_manifests(_record(), namespace="devai-sbx-sb1234")[2]
    dns_rule = policy["spec"]["egress"][0]
    assert {"ipBlock": {"cidr": "10.30.0.10/32"}} in dns_rule["to"]
    assert {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}}} in dns_rule["to"]


def test_dns_egress_survives_a_missing_resolv_conf(monkeypatch):
    from devai.sandbox import isolation
    from devai.sandbox.isolation import build_isolation_manifests

    monkeypatch.setattr(isolation, "_RESOLV_CONF", "/nonexistent/resolv.conf")

    policy = build_isolation_manifests(_record(), namespace="devai-sbx-sb1234")[2]
    dns_rule = policy["spec"]["egress"][0]
    assert {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}}} in dns_rule["to"]
