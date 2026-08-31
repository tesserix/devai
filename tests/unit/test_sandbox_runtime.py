"""Sandbox boundary over the existing Job runtime (#180).

The rule under test throughout: a sandbox runs the *same* runner image and the
*same* Job shape as production, and differs only in its boundaries.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from devai.runtime.job_spec import RunnerJobInputs, build_job_spec
from devai.runtime.k8s_client import RuntimeConfig
from devai.sandbox.isolation import build_isolation_manifests
from devai.sandbox.job import (
    SANDBOX_CONFIG_KEY,
    SANDBOX_LABEL,
    apply_sandbox_boundary,
    sandbox_from_stage_config,
)
from devai.sandbox.models import (
    AgentRef,
    ImportSnapshot,
    ModelRef,
    PromptRef,
    SandboxLimits,
    SandboxRecord,
    SandboxSpec,
    SandboxStatus,
    ToolMode,
    ToolPolicy,
)
from devai.sandbox.portable import build_portable_runtime_manifests, portable_runtime_endpoint
from devai.sandbox.provisioner import SandboxProvisioner

_NS = "devai"


def _cfg() -> RuntimeConfig:
    return RuntimeConfig(
        namespace=_NS,
        runner_image="example.dev/devai-runner:test",
        runner_image_per_stack={},
        preview_domain="example.app",
        preview_namespace="devai-previews",
        registry_url="http://aregistry:12121",
        pull_secret_name=None,
        service_account_name="devai-runner",
        default_ttl_seconds=3600,
        default_backoff_limit=2,
        pod_security_context={},
    )


def _spec(**over: Any) -> SandboxSpec:
    base: dict[str, Any] = {
        "agent": AgentRef(name="senior_developer", version="v1.8.2"),
        "model": ModelRef(provider="anthropic", model="claude-sonnet-4-20250514"),
    }
    return SandboxSpec(**{**base, **over})


def _record(spec: SandboxSpec | None = None, sandbox_id: str = "sb-123") -> SandboxRecord:
    now = datetime.now(UTC)
    return SandboxRecord(
        id=sandbox_id,
        owner="sam@example.com",
        spec=spec or _spec(),
        status=SandboxStatus.PENDING,
        created_at=now,
        expires_at=now + timedelta(hours=4),
    )


def _imported_record(sandbox_id: str = "sb-portable") -> SandboxRecord:
    snapshot = ImportSnapshot(
        import_id="bf2ef27d-98a2-4ce4-b87a-c6952d2d5d09",
        registry_ref="registry://acme/agents/acme/support@1.4.0",
        agent_digest="sha256:" + "a" * 64,
        dependency_lock=[],
        runtime={
            "type": "container",
            "protocol": "a2a",
            "image": "ghcr.io/acme/support@sha256:" + "c" * 64,
            "port": 8080,
            "path": "/a2a/v1",
        },
        permissions={},
    )
    return _record(
        SandboxSpec(
            agent=AgentRef(name="support", version="1.4.0"),
            import_id=snapshot.import_id,
            import_snapshot=snapshot,
            model=ModelRef(provider="anthropic", model="claude-sonnet-4-20250514"),
        ),
        sandbox_id,
    )


def _job(record: SandboxRecord | None = None) -> dict[str, Any]:
    inputs = RunnerJobInputs(
        task_id="task-abc",
        stage_name="implement",
        agent_name="senior_developer",
        image="",
        repo="tesserix/devai",
        intent="add a health endpoint",
        blueprint="feature",
        extra_env={},
    )
    return apply_sandbox_boundary(build_job_spec(_cfg(), inputs), record or _record())


def _env(job: dict[str, Any]) -> dict[str, Any]:
    container = job["spec"]["template"]["spec"]["containers"][0]
    return {e["name"]: e for e in container["env"]}


# ── the runtime is unchanged ──────────────────────────────────────────────


def test_the_sandbox_runs_the_same_runner_image_and_command() -> None:
    plain = build_job_spec(
        _cfg(),
        RunnerJobInputs(
            task_id="task-abc",
            stage_name="implement",
            agent_name="senior_developer",
            image="",
            repo="tesserix/devai",
            intent="add a health endpoint",
            blueprint="feature",
            extra_env={},
        ),
    )
    sandboxed = _job()

    plain_c = plain["spec"]["template"]["spec"]["containers"][0]
    sb_c = sandboxed["spec"]["template"]["spec"]["containers"][0]
    assert sb_c["image"] == plain_c["image"]
    assert sb_c["command"] == plain_c["command"]


def test_the_boundary_does_not_mutate_the_job_it_was_given() -> None:
    inputs = RunnerJobInputs(
        task_id="task-abc",
        stage_name="implement",
        agent_name="senior_developer",
        image="",
        repo="tesserix/devai",
        intent="i",
        blueprint="feature",
        extra_env={},
    )
    original = build_job_spec(_cfg(), inputs)
    before = original["spec"]["template"]["spec"]["serviceAccountName"]

    apply_sandbox_boundary(original, _record())

    assert original["spec"]["template"]["spec"]["serviceAccountName"] == before


# ── no production secrets ─────────────────────────────────────────────────


def test_production_secrets_never_reach_a_sandbox() -> None:
    env = _env(_job())
    referenced = {
        e["valueFrom"]["secretKeyRef"]["name"]
        for e in env.values()
        if "valueFrom" in e and "secretKeyRef" in e["valueFrom"]
    }
    assert "devai-api-secrets" not in referenced


def test_credentials_come_from_a_sandbox_scoped_secret() -> None:
    env = _env(_job())
    key = env["DEVAI_ANTHROPIC_API_KEY"]["valueFrom"]["secretKeyRef"]
    assert key["name"] == "devai-sandbox-sb-123"
    assert key["optional"] is True


def test_the_service_account_is_the_scoped_one() -> None:
    pod = _job()["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "devai-sandbox"
    assert pod["automountServiceAccountToken"] is False


# ── the spec is pinned into the pod ───────────────────────────────────────


def test_the_pinned_model_is_injected() -> None:
    env = _env(_job())
    assert env["DEVAI_LLM_PROVIDER"]["value"] == "anthropic"
    assert env["DEVAI_SANDBOX_MODEL"]["value"] == "claude-sonnet-4-20250514"


def test_tool_modes_are_injected_so_the_gateway_can_enforce_them() -> None:
    spec = _spec(tools=ToolPolicy(default_mode=ToolMode.BLOCK, overrides={"read_file": ToolMode.REAL}))
    env = _env(_job(_record(spec)))
    assert env["DEVAI_SANDBOX_TOOL_MODE"]["value"] == "block"
    assert '"read_file": "real"' in env["DEVAI_SANDBOX_TOOL_OVERRIDES"]["value"]


def test_the_pinned_runtime_version_is_injected_only_when_set() -> None:
    assert "DEVAI_ADK_VERSION" not in _env(_job())
    assert _env(_job(_record(_spec(adk_version="0.1.1"))))["DEVAI_ADK_VERSION"]["value"] == "0.1.1"


def test_the_pinned_prompt_is_injected_only_when_set() -> None:
    assert "DEVAI_SANDBOX_PROMPT" not in _env(_job())
    spec = _spec(prompt=PromptRef(ref="impl-prompt", version="v3"))
    assert _env(_job(_record(spec)))["DEVAI_SANDBOX_PROMPT"]["value"] == "impl-prompt@v3"


def test_budget_limits_are_injected() -> None:
    spec = _spec(limits=SandboxLimits(max_tokens=1234, max_cost_usd=2.5, max_wall_clock_s=60))
    env = _env(_job(_record(spec)))
    assert env["DEVAI_SANDBOX_MAX_TOKENS"]["value"] == "1234"
    assert env["DEVAI_SANDBOX_MAX_COST_USD"]["value"] == "2.5"


def test_the_sandbox_id_is_labelled_and_injected() -> None:
    job = _job()
    assert job["metadata"]["labels"][SANDBOX_LABEL] == "sb-123"
    assert job["spec"]["template"]["metadata"]["labels"][SANDBOX_LABEL] == "sb-123"
    assert _env(job)["DEVAI_SANDBOX_ID"]["value"] == "sb-123"


# ── nothing lingers, nothing retries ──────────────────────────────────────


def test_wall_clock_limit_becomes_the_pod_deadline() -> None:
    spec = _spec(limits=SandboxLimits(max_wall_clock_s=60))
    job = _job(_record(spec))
    assert job["spec"]["activeDeadlineSeconds"] == 60


def test_a_sandbox_job_does_not_retry() -> None:
    # A retried eval would silently double the cost and blur the trajectory.
    assert _job()["spec"]["backoffLimit"] == 0


def test_finished_sandbox_jobs_are_cleaned_up() -> None:
    assert _job()["spec"]["ttlSecondsAfterFinished"] > 0


def test_the_pod_runs_restricted() -> None:
    pod = _job()["spec"]["template"]["spec"]
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
    sc = pod["containers"][0]["securityContext"]
    assert sc["allowPrivilegeEscalation"] is False
    assert sc["capabilities"]["drop"] == ["ALL"]


# ── per-sandbox namespace on the boundary ─────────────────────────────────


def _ns_record() -> SandboxRecord:
    record = _record()
    record.detail["namespace"] = "devai-sbx-x"
    return record


def test_boundary_stamps_record_namespace() -> None:
    job = _job(_ns_record())
    assert job["metadata"]["namespace"] == "devai-sbx-x"
    assert "devai-sbx-x.svc" in _env(job)["HTTP_PROXY"]["value"]


def test_boundary_legacy_record_keeps_job_namespace() -> None:
    # No recorded namespace → the job keeps the control-plane one it was built with.
    assert _job()["metadata"]["namespace"] == "devai"


def test_boundary_readonly_rootfs() -> None:
    job = _job()
    container = job["spec"]["template"]["spec"]["containers"][0]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert {"name": "tmp", "mountPath": "/tmp"} in container["volumeMounts"]
    assert {"name": "tmp", "emptyDir": {}} in job["spec"]["template"]["spec"]["volumes"]
    assert _env(job)["HOME"]["value"] == "/devai/work"


def test_boundary_runtime_class(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEVAI_SANDBOX_RUNTIME_CLASS", "gvisor")
    assert _job()["spec"]["template"]["spec"]["runtimeClassName"] == "gvisor"
    monkeypatch.delenv("DEVAI_SANDBOX_RUNTIME_CLASS")
    assert "runtimeClassName" not in _job()["spec"]["template"]["spec"]


# ── dispatch hook ─────────────────────────────────────────────────────────


def test_a_stage_without_a_sandbox_dispatches_normally() -> None:
    assert sandbox_from_stage_config({"depth": "3"}) is None


def test_a_serialized_sandbox_on_the_stage_config_is_honoured() -> None:
    record = _record()
    config = {SANDBOX_CONFIG_KEY: record.model_dump(mode="json")}

    assert sandbox_from_stage_config(config).id == record.id


def test_a_malformed_sandbox_pin_refuses_to_run_unsandboxed() -> None:
    with pytest.raises(ValueError, match="invalid sandbox"):
        sandbox_from_stage_config({SANDBOX_CONFIG_KEY: {"id": "sb-1"}})


def test_the_sandbox_pin_is_not_forwarded_to_the_runner_as_stage_env() -> None:
    # JobRunnerStage drops `__`-prefixed config keys when it builds the stage
    # env; the pin is dispatcher-internal and must not travel to the pod.
    stage_config = {SANDBOX_CONFIG_KEY: _record().model_dump(mode="json"), "depth": "3"}
    forwarded = {k: str(v) for k, v in stage_config.items() if not k.startswith("__")}

    assert forwarded == {"depth": "3"}


# ── isolation manifests ───────────────────────────────────────────────────


def _by_kind(manifests: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {m["kind"]: m for m in manifests}


def test_isolation_ships_a_quota_a_limitrange_and_a_network_policy() -> None:
    kinds = set(_by_kind(build_isolation_manifests(_record(), namespace=_NS)))
    assert kinds == {"ResourceQuota", "LimitRange", "NetworkPolicy"}


def test_egress_is_default_deny_with_an_explicit_allow_list() -> None:
    np = _by_kind(build_isolation_manifests(_record(), namespace=_NS))["NetworkPolicy"]
    assert "Egress" in np["spec"]["policyTypes"]
    assert np["spec"]["podSelector"]["matchLabels"][SANDBOX_LABEL] == "sb-123"
    # An empty egress list would be a silent allow-all in some CNIs; DNS must be
    # explicit and the rest of the list is the allow-list.
    assert np["spec"]["egress"], "egress rules must be explicit"
    ports = [p["port"] for rule in np["spec"]["egress"] for p in rule.get("ports", [])]
    assert 53 in ports


def test_every_isolation_object_is_scoped_to_this_sandbox() -> None:
    for m in build_isolation_manifests(_record(), namespace=_NS):
        assert m["metadata"]["namespace"] == _NS
        assert m["metadata"]["name"].endswith("sb-123")
        assert m["metadata"]["labels"][SANDBOX_LABEL] == "sb-123"


def test_portable_container_runtime_uses_only_the_pinned_image_and_restricted_identity() -> None:
    manifests = build_portable_runtime_manifests(_imported_record(), namespace=_NS)
    by_kind = _by_kind(manifests)
    deployment = by_kind["Deployment"]
    pod = deployment["spec"]["template"]["spec"]
    container = pod["containers"][0]

    assert container["image"] == "ghcr.io/acme/support@sha256:" + "c" * 64
    assert pod["serviceAccountName"] == "devai-sandbox"
    assert pod["automountServiceAccountToken"] is False
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    assert by_kind["Service"]["spec"]["ports"][0]["targetPort"] == 8080
    assert portable_runtime_endpoint(_imported_record(), namespace=_NS).endswith(":8080/a2a/v1")


# ── provisioner ───────────────────────────────────────────────────────────


class _FakeRuntime:
    """Records what the provisioner would create/delete in the cluster."""

    def __init__(self, *, fail: bool = False) -> None:
        self.created: list[dict[str, Any]] = []
        self.deleted: list[tuple[str, str]] = []
        self._fail = fail
        self.config = _cfg()

    async def connect(self) -> None:
        return None

    async def apply_manifest(self, manifest: dict[str, Any]) -> None:
        if self._fail:
            raise RuntimeError("cluster unavailable")
        self.created.append(manifest)

    async def delete_manifest(self, kind: str, name: str, namespace: str) -> None:
        self.deleted.append((kind, name))


class _FakeStore:
    def __init__(self) -> None:
        self.statuses: list[str] = []

    async def set_sandbox_status(self, sandbox_id: str, status: str, detail: dict[str, Any] | None = None) -> None:
        self.statuses.append(status)


@pytest.mark.asyncio
async def test_provisioning_walks_pending_to_ready() -> None:
    runtime, store = _FakeRuntime(), _FakeStore()

    record = await SandboxProvisioner(runtime, store).provision(_record())

    assert store.statuses == ["provisioning", "ready"]
    assert record.status is SandboxStatus.READY
    # quota, limits, egress policy, the sandbox's own Secret, then the proxy's
    # configmap/pod/service
    assert len(runtime.created) == 7


@pytest.mark.asyncio
async def test_provisioning_materializes_an_imported_container_runtime() -> None:
    runtime, store = _FakeRuntime(), _FakeStore()

    record = await SandboxProvisioner(runtime, store).provision(_imported_record())

    assert {manifest["kind"] for manifest in runtime.created} >= {"Deployment", "Service"}
    assert record.detail["agent_runtime"]["endpoint"].endswith(":8080/a2a/v1")
    assert record.detail["agent_runtime"]["agent_digest"] == "sha256:" + "a" * 64


@pytest.mark.asyncio
async def test_a_failed_provision_lands_in_failed_not_ready() -> None:
    runtime, store = _FakeRuntime(fail=True), _FakeStore()

    record = await SandboxProvisioner(runtime, store).provision(_record())

    assert store.statuses[-1] == "failed"
    assert record.status is SandboxStatus.FAILED
    assert "cluster unavailable" in str(record.detail)


@pytest.mark.asyncio
async def test_teardown_removes_every_isolation_object() -> None:
    runtime, store = _FakeRuntime(), _FakeStore()
    provisioner = SandboxProvisioner(runtime, store)
    await provisioner.provision(_record())

    await provisioner.teardown(_record())

    assert {kind for kind, _ in runtime.deleted} == {
        "ResourceQuota",
        "LimitRange",
        "NetworkPolicy",
        "ConfigMap",
        "Pod",
        "Service",
        "Secret",
    }
    assert store.statuses[-1] == "destroyed"


# ── cluster client dispatch ───────────────────────────────────────────────


class _FakeApi:
    def __init__(self, *, conflict_on_create: bool = False) -> None:
        self.calls: list[str] = []
        self._conflict = conflict_on_create

    def __getattr__(self, name: str) -> Any:
        async def call(**kwargs: Any) -> None:
            self.calls.append(name)
            if name.startswith("create_") and self._conflict:
                raise _Conflict()

        return call


class _Conflict(Exception):
    status = 409


def _runtime_with(api: _FakeApi) -> Any:
    from devai.runtime.k8s_client import K8sJobRuntime

    runtime = K8sJobRuntime(_cfg())
    runtime._core_v1 = api  # noqa: SLF001 — standing in for a connected cluster
    runtime._networking_v1 = api  # noqa: SLF001
    return runtime


@pytest.mark.asyncio
async def test_each_isolation_kind_maps_to_its_api() -> None:
    api = _FakeApi()
    runtime = _runtime_with(api)

    for manifest in build_isolation_manifests(_record(), namespace=_NS):
        await runtime.apply_manifest(manifest)

    assert api.calls == [
        "create_namespaced_resource_quota",
        "create_namespaced_limit_range",
        "create_namespaced_network_policy",
    ]


@pytest.mark.asyncio
async def test_an_existing_object_is_patched_not_re_raised() -> None:
    api = _FakeApi(conflict_on_create=True)

    await _runtime_with(api).apply_manifest(build_isolation_manifests(_record(), namespace=_NS)[0])

    assert api.calls[-1] == "patch_namespaced_resource_quota"


@pytest.mark.asyncio
async def test_an_unsupported_kind_is_refused() -> None:
    with pytest.raises(ValueError, match="unsupported manifest kind"):
        await _runtime_with(_FakeApi()).apply_manifest({"kind": "ClusterRole", "metadata": {"name": "x"}})


@pytest.mark.asyncio
async def test_teardown_is_idempotent_when_the_objects_are_already_gone() -> None:
    class _GoneRuntime(_FakeRuntime):
        async def delete_manifest(self, kind: str, name: str, namespace: str) -> None:
            raise RuntimeError("not found")

    store = _FakeStore()
    await SandboxProvisioner(_GoneRuntime(), store).teardown(_record())

    assert store.statuses[-1] == "destroyed"


def test_the_workspace_kinds_are_routable_to_a_k8s_api() -> None:
    from devai.runtime.k8s_client import K8sJobRuntime

    rt = K8sJobRuntime.__new__(K8sJobRuntime)
    rt._core_v1 = object()  # noqa: SLF001
    rt._networking_v1 = object()  # noqa: SLF001

    for kind, suffix in (
        ("PersistentVolumeClaim", "persistent_volume_claim"),
        ("Secret", "secret"),
        ("Pod", "pod"),
        ("Service", "service"),
    ):
        assert rt._manifest_api(kind)[1] == suffix  # noqa: SLF001


# ── per-sandbox namespace isolation ───────────────────────────────────────


def test_isolation_quota_is_namespace_wide() -> None:
    quota = _by_kind(build_isolation_manifests(_record(), namespace="devai-sbx-x"))["ResourceQuota"]
    # The namespace is the fence now; a PriorityClass scope split the shared
    # namespace, which no longer exists.
    assert "scopeSelector" not in quota["spec"]


def test_network_policy_egress_targets_control_plane_pods_only() -> None:
    np = _by_kind(
        build_isolation_manifests(_record(), namespace="devai-sbx-x", control_plane_namespace="devai")
    )["NetworkPolicy"]
    rules = np["spec"]["egress"]
    own = rules[1]["to"][0]["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"]
    assert own == "devai-sbx-x"
    cp = rules[2]["to"][0]
    assert cp["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"] == "devai"
    # Pods only — Postgres/Redis in the control-plane namespace stay dark.
    assert cp["podSelector"]["matchLabels"]["app.kubernetes.io/name"] == "devai"


def test_network_policy_ingress_is_control_plane_and_own_namespace() -> None:
    np = _by_kind(
        build_isolation_manifests(_record(), namespace="devai-sbx-x", control_plane_namespace="devai")
    )["NetworkPolicy"]
    frm = np["spec"]["ingress"][0]["from"]
    assert {"podSelector": {}} in frm  # any pod in the sandbox's own namespace
    cp = next(e for e in frm if "namespaceSelector" in e)
    assert cp["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"] == "devai"
    assert cp["podSelector"]["matchLabels"]["app.kubernetes.io/name"] == "devai"
