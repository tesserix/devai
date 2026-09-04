"""Sandbox contract + service (#179).

A sandbox pins an immutable configuration — agent version, model, prompt version,
tool modes, dataset version, limits, TTL — so "which configuration was this agent
evaluated under?" always has an answer. Safety defaults matter more than
convenience here: an unspecified tool is mocked, never live.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from devai.sandbox.models import SandboxSpec, SandboxStatus, ToolMode
from devai.sandbox.service import SandboxError, SandboxService, _to_record

_MIN_SPEC: dict[str, Any] = {
    "agent": {"name": "code-remediator-agent", "version": "v1.8.2"},
    "model": {"provider": "anthropic", "model": "claude-sonnet-4-20250514"},
}


class _FakeDB:
    """Stands in for services.database.Database — only the sandbox surface."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.imports: dict[str, dict[str, Any]] = {}

    async def create_sandbox(self, **kw: Any) -> dict[str, Any]:
        self.rows[kw["sandbox_id"]] = {**kw, "id": kw["sandbox_id"]}
        return self.rows[kw["sandbox_id"]]

    async def get_sandbox(self, sandbox_id: str) -> dict[str, Any] | None:
        return self.rows.get(sandbox_id)

    async def list_sandboxes(self, owner: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        rows = list(self.rows.values())
        if owner is not None:
            rows = [r for r in rows if r["owner"] == owner]
        return rows[:limit]

    async def set_sandbox_status(self, sandbox_id: str, status: str, detail: dict[str, Any] | None = None) -> None:
        if sandbox_id in self.rows:
            self.rows[sandbox_id]["status"] = status
            if detail:
                self.rows[sandbox_id]["detail"] = detail

    async def touch_sandbox(self, sandbox_id: str, ttl_seconds: int) -> None:
        now = datetime.now(UTC)
        self.rows[sandbox_id]["last_access_at"] = now
        self.rows[sandbox_id]["expires_at"] = now + timedelta(seconds=ttl_seconds)

    async def expired_sandboxes(self, now: datetime) -> list[dict[str, Any]]:
        return [r for r in self.rows.values() if r["status"] != "destroyed" and r["expires_at"] <= now]

    async def get_agent_import(self, owner_scope: str, import_id: str) -> dict[str, Any] | None:
        row = self.imports.get(import_id)
        return row if row and row["owner_scope"] == owner_scope else None


def _service(db: Any = None, **kw: Any) -> SandboxService:
    return SandboxService(db or _FakeDB(), **kw)


# ── the contract ──────────────────────────────────────────────────────


def test_minimal_spec_defaults_to_safe_boundaries() -> None:
    spec = SandboxSpec.model_validate(_MIN_SPEC)
    assert spec.tools.default_mode is ToolMode.MOCK  # never live by omission
    assert spec.ttl_seconds == 4 * 60 * 60
    assert spec.limits.max_cost_usd > 0
    assert spec.limits.max_tokens > 0
    assert spec.credentials.llm_connector == ""
    assert spec.credentials.confirmed is False


@pytest.mark.parametrize(
    "credentials",
    [
        {"llm_connector": "sandbox-evals", "confirmed": False},
        {"llm_connector": "", "confirmed": True},
    ],
)
def test_connector_name_and_confirmation_have_to_be_supplied_together(credentials: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="connector"):
        SandboxSpec.model_validate({**_MIN_SPEC, "credentials": credentials})


def test_spec_is_frozen_so_a_pinned_configuration_cannot_drift() -> None:
    spec = SandboxSpec.model_validate(_MIN_SPEC)
    with pytest.raises(ValidationError):
        spec.ttl_seconds = 99


def test_unknown_tool_mode_is_rejected() -> None:
    with pytest.raises(ValidationError):
        SandboxSpec.model_validate({**_MIN_SPEC, "tools": {"default_mode": "yolo"}})


def test_per_tool_override_beats_the_default_mode() -> None:
    spec = SandboxSpec.model_validate(
        {**_MIN_SPEC, "tools": {"default_mode": "block", "overrides": {"scm_list_files": "real"}}}
    )
    assert spec.tools.mode_for("scm_list_files") is ToolMode.REAL
    assert spec.tools.mode_for("anything_else") is ToolMode.BLOCK


@pytest.mark.parametrize(
    "patch",
    [
        {"ttl_seconds": 0},
        {"ttl_seconds": -1},
        {"limits": {"max_cost_usd": 0}},
        {"limits": {"max_tokens": -5}},
        {"agent": {"name": "a", "version": ""}},
        {"agent": {"name": "", "version": "v1"}},
    ],
)
def test_meaningless_spec_values_are_rejected(patch: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        SandboxSpec.model_validate({**_MIN_SPEC, **patch})


def test_ttl_is_capped_so_a_sandbox_cannot_live_forever() -> None:
    with pytest.raises(ValidationError):
        SandboxSpec.model_validate({**_MIN_SPEC, "ttl_seconds": 60 * 60 * 24 * 30})


def test_browser_requires_a_workspace() -> None:
    with pytest.raises(ValidationError, match="browser.*workspace"):
        SandboxSpec.model_validate({**_MIN_SPEC, "browser": True})


def test_browser_can_be_enabled_for_a_workspace() -> None:
    spec = SandboxSpec.model_validate({**_MIN_SPEC, "workspace": True, "browser": True})

    assert spec.workspace is True
    assert spec.browser is True


# ── lifecycle ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_pins_the_spec_and_sets_an_expiry() -> None:
    db = _FakeDB()
    before = datetime.now(UTC)
    rec = await _service(db).create(SandboxSpec.model_validate(_MIN_SPEC), owner="sam@example.com")

    assert rec.status is SandboxStatus.PENDING
    assert rec.owner == "sam@example.com"
    assert rec.spec.agent.version == "v1.8.2"
    assert before + timedelta(hours=3) < rec.expires_at < before + timedelta(hours=5)
    assert db.rows[rec.id]["spec"]["model"]["provider"] == "anthropic"


@pytest.mark.asyncio
async def test_create_passes_tenant_quota_context_to_the_atomic_database_write() -> None:
    db = _FakeDB()
    settings = type(
        "Settings",
        (),
        {"sandbox_max_live_per_tenant": 5, "sandbox_monthly_cost_limit_usd": 25.0},
    )()

    rec = await _service(db, settings=settings).create(
        SandboxSpec.model_validate(_MIN_SPEC),
        owner="tenant-a:subject-a",
        tenant_id="tenant-a",
        user_id="subject-a",
    )

    row = db.rows[rec.id]
    assert row["tenant_id"] == "tenant-a"
    assert row["user_id"] == "subject-a"
    assert row["max_live_per_tenant"] == 5
    assert row["monthly_cost_limit_usd"] == 25.0


@pytest.mark.asyncio
async def test_retrying_a_deterministic_sandbox_creation_returns_the_original_row() -> None:
    db = _FakeDB()
    service = _service(db)
    spec = SandboxSpec.model_validate(_MIN_SPEC)

    first = await service.create(spec, owner="tenant-a:alice", sandbox_id="sbx-stable")
    second = await service.create(spec, owner="tenant-a:alice", sandbox_id="sbx-stable")

    assert first.id == second.id == "sbx-stable"
    assert len(db.rows) == 1


@pytest.mark.asyncio
async def test_a_deterministic_sandbox_id_cannot_be_reused_for_different_input() -> None:
    service = _service()
    await service.create(SandboxSpec.model_validate(_MIN_SPEC), owner="tenant-a:alice", sandbox_id="sbx-stable")

    with pytest.raises(SandboxError, match="different sandbox request"):
        await service.create(
            SandboxSpec.model_validate({**_MIN_SPEC, "ttl_seconds": 60}),
            owner="tenant-a:alice",
            sandbox_id="sbx-stable",
        )


def _portable_import(import_id: str) -> dict[str, Any]:
    return {
        "id": import_id,
        "owner_scope": "tenant-a",
        "registry_ref": "registry://acme/agents/acme/support@1.4.0",
        "state": "ready",
        "agent": {
            "name": "support",
            "namespace": "acme",
            "version": "1.4.0",
            "digest": "sha256:" + "a" * 64,
            "runtime": {
                "type": "container",
                "protocol": "a2a",
                "image": "ghcr.io/acme/support@sha256:" + "c" * 64,
            },
        },
        "dependency_lock": [
            {
                "kind": "Skill",
                "name": "triage",
                "namespace": "acme",
                "version": "1.0.0",
                "digest": "sha256:" + "b" * 64,
            }
        ],
        "permissions": {
            "network": {"domains": ["api.example.com"]},
            "scopes": ["acme/support"],
            "workspace": True,
            "tools": {"real": ["ticket_lookup"]},
        },
        "conformance": {"level": "sandbox_runnable"},
    }


@pytest.mark.asyncio
async def test_create_from_import_copies_the_exact_lock_into_the_sandbox() -> None:
    db = _FakeDB()
    import_id = "bf2ef27d-98a2-4ce4-b87a-c6952d2d5d09"
    db.imports[import_id] = _portable_import(import_id)
    spec = SandboxSpec.model_validate(
        {
            "import_id": import_id,
            "model": _MIN_SPEC["model"],
            "workspace": True,
            "allow_domains": ["api.example.com"],
            "allow_scopes": ["acme/support"],
            "tools": {"overrides": {"ticket_lookup": "real"}},
        }
    )

    record = await _service(db).create(
        spec,
        owner="tenant-a:subject-a",
        tenant_id="tenant-a",
        user_id="subject-a",
    )

    assert record.spec.agent.name == "support"
    assert record.spec.agent.version == "1.4.0"
    assert record.spec.import_snapshot.agent_digest == "sha256:" + "a" * 64
    assert record.spec.import_snapshot.product_id == "acme"
    assert record.spec.import_snapshot.dependency_lock[0]["version"] == "1.0.0"
    assert db.rows[record.id]["spec"]["import_snapshot"]["runtime"]["type"] == "container"


@pytest.mark.asyncio
async def test_imported_sandbox_cannot_widen_locked_permissions() -> None:
    db = _FakeDB()
    import_id = "bf2ef27d-98a2-4ce4-b87a-c6952d2d5d09"
    db.imports[import_id] = _portable_import(import_id)
    spec = SandboxSpec.model_validate(
        {
            "import_id": import_id,
            "model": _MIN_SPEC["model"],
            "allow_domains": ["metadata.google.internal"],
        }
    )

    with pytest.raises(SandboxError, match="network domains"):
        await _service(db).create(
            spec,
            owner="tenant-a:subject-a",
            tenant_id="tenant-a",
            user_id="subject-a",
        )


@pytest.mark.asyncio
async def test_import_lookup_is_tenant_scoped_and_foreign_import_is_hidden() -> None:
    db = _FakeDB()
    import_id = "bf2ef27d-98a2-4ce4-b87a-c6952d2d5d09"
    db.imports[import_id] = _portable_import(import_id)
    spec = SandboxSpec.model_validate({"import_id": import_id, "model": _MIN_SPEC["model"]})

    with pytest.raises(SandboxError, match="not found"):
        await _service(db).create(
            spec,
            owner="tenant-b:subject-b",
            tenant_id="tenant-b",
            user_id="subject-b",
        )


@pytest.mark.asyncio
async def test_tenant_quota_rejection_is_a_clear_sandbox_error() -> None:
    from devai.adapters.telemetry import NoopTelemetryAdapter, set_global_telemetry
    from devai.services.database import SandboxQuotaExceeded

    class _QuotaDB(_FakeDB):
        async def create_sandbox(self, **kw: Any) -> dict[str, Any]:
            del kw
            raise SandboxQuotaExceeded("tenant tenant-a reached its concurrent sandbox quota (5)")

    class _Telemetry(NoopTelemetryAdapter):
        counters: list[tuple[str, dict[str, str]]] = []

        def incr(self, name: str, value: float = 1.0, attrs: dict[str, str] | None = None) -> None:
            assert value == 1.0
            self.counters.append((name, dict(attrs or {})))

    telemetry = _Telemetry()
    set_global_telemetry(telemetry)
    try:
        with pytest.raises(SandboxError, match="tenant-a.*concurrent sandbox quota"):
            await _service(_QuotaDB()).create(
                SandboxSpec.model_validate(_MIN_SPEC),
                owner="tenant-a:subject-a",
                tenant_id="tenant-a",
                user_id="subject-a",
            )
    finally:
        set_global_telemetry(None)

    assert telemetry.counters == [("devai.sandbox.quota_rejections", {"quota": "concurrent"})]


@pytest.mark.asyncio
async def test_get_hides_another_owners_sandbox() -> None:
    svc = _service()
    rec = await svc.create(SandboxSpec.model_validate(_MIN_SPEC), owner="sam@example.com")

    assert await svc.get(rec.id, owner="mallory@example.com") is None
    assert await svc.get(rec.id, owner="sam@example.com") is not None


@pytest.mark.asyncio
async def test_admin_sees_every_sandbox() -> None:
    svc = _service()
    rec = await svc.create(SandboxSpec.model_validate(_MIN_SPEC), owner="sam@example.com")

    assert await svc.get(rec.id, owner="ops@example.com", is_admin=True) is not None
    assert len(await svc.list(owner="ops@example.com", is_admin=True)) == 1


@pytest.mark.asyncio
async def test_list_is_owner_scoped() -> None:
    svc = _service()
    await svc.create(SandboxSpec.model_validate(_MIN_SPEC), owner="sam@example.com")
    await svc.create(SandboxSpec.model_validate(_MIN_SPEC), owner="other@example.com")

    assert len(await svc.list(owner="sam@example.com")) == 1


@pytest.mark.asyncio
async def test_destroy_is_owner_scoped_and_terminal() -> None:
    svc = _service()
    rec = await svc.create(SandboxSpec.model_validate(_MIN_SPEC), owner="sam@example.com")

    with pytest.raises(SandboxError):
        await svc.destroy(rec.id, owner="mallory@example.com")

    await svc.destroy(rec.id, owner="sam@example.com")
    assert (await svc.get(rec.id, owner="sam@example.com")).status is SandboxStatus.DESTROYED


@pytest.mark.asyncio
async def test_expired_sandboxes_are_reaped() -> None:
    db = _FakeDB()
    svc = _service(db)
    rec = await svc.create(SandboxSpec.model_validate({**_MIN_SPEC, "ttl_seconds": 60}), owner="sam@example.com")
    db.rows[rec.id]["expires_at"] = datetime.now(UTC) - timedelta(seconds=1)

    assert await svc.reap_expired() == 1
    assert db.rows[rec.id]["status"] == "destroyed"
    assert await svc.reap_expired() == 0  # already destroyed — not reaped twice


@pytest.mark.asyncio
async def test_activity_resets_the_sandbox_idle_deadline() -> None:
    db = _FakeDB()
    svc = _service(db)
    rec = await svc.create(SandboxSpec.model_validate(_MIN_SPEC), owner="sam@example.com")
    previous_deadline = rec.expires_at

    await svc.touch(rec.id)

    assert db.rows[rec.id]["last_access_at"] > rec.last_access_at
    assert db.rows[rec.id]["expires_at"] > previous_deadline
    assert db.rows[rec.id]["expires_at"] - db.rows[rec.id]["last_access_at"] == timedelta(hours=4)


# ── provisioning ──────────────────────────────────────────────────────


class _FakeProvisioner:
    def __init__(self) -> None:
        self.provisioned: list[str] = []
        self.torn_down: list[str] = []

    async def provision(self, record: Any) -> Any:
        self.provisioned.append(record.id)
        return record.model_copy(update={"status": SandboxStatus.READY})

    async def teardown(self, record: Any) -> Any:
        self.torn_down.append(record.id)
        return record.model_copy(update={"status": SandboxStatus.DESTROYED})


@pytest.mark.asyncio
async def test_create_provisions_and_returns_a_ready_sandbox() -> None:
    prov = _FakeProvisioner()

    rec = await _service(provisioner=prov).create(SandboxSpec.model_validate(_MIN_SPEC), owner="sam@example.com")

    assert prov.provisioned == [rec.id]
    assert rec.status is SandboxStatus.READY


@pytest.mark.asyncio
async def test_destroy_tears_down_the_cluster_objects() -> None:
    prov = _FakeProvisioner()
    svc = _service(provisioner=prov)
    rec = await svc.create(SandboxSpec.model_validate(_MIN_SPEC), owner="sam@example.com")

    await svc.destroy(rec.id, owner="sam@example.com")

    assert prov.torn_down == [rec.id]


@pytest.mark.asyncio
async def test_reaping_tears_down_too_so_nothing_lingers() -> None:
    db, prov = _FakeDB(), _FakeProvisioner()
    svc = _service(db, provisioner=prov)
    rec = await svc.create(SandboxSpec.model_validate({**_MIN_SPEC, "ttl_seconds": 60}), owner="sam@example.com")
    db.rows[rec.id]["expires_at"] = datetime.now(UTC) - timedelta(seconds=1)

    assert await svc.reap_expired() == 1
    assert prov.torn_down == [rec.id]


@pytest.mark.asyncio
async def test_a_provisioning_failure_leaves_the_sandbox_readable() -> None:
    class _Broken:
        async def provision(self, record: Any) -> Any:
            raise RuntimeError("cluster unavailable")

    svc = _service(provisioner=_Broken())
    rec = await svc.create(SandboxSpec.model_validate(_MIN_SPEC), owner="sam@example.com")

    assert rec.status is SandboxStatus.FAILED
    assert (await svc.get(rec.id, owner="sam@example.com")) is not None


# ── orphan-namespace reaper ───────────────────────────────────────────


class _NsRuntime:
    def __init__(self, namespaces: list[dict[str, Any]]) -> None:
        self.namespaces = namespaces
        self.deleted: list[str] = []

    async def list_namespaces(self, label_selector: str) -> list[dict[str, Any]]:
        return self.namespaces

    async def delete_namespace(self, name: str) -> None:
        self.deleted.append(name)


def _ns(name: str, sandbox_id: str, phase: str = "Active") -> dict[str, Any]:
    return {
        "metadata": {"name": name, "labels": {"devai.tesserix.app/sandbox": sandbox_id}},
        "status": {"phase": phase},
    }


@pytest.mark.asyncio
async def test_reap_orphan_namespaces_deletes_rowless_namespace() -> None:
    db = _FakeDB()
    svc = _service(db, provisioner=_FakeProvisioner())
    rec = await svc.create(SandboxSpec.model_validate(_MIN_SPEC), owner="sam@example.com")
    runtime = _NsRuntime([_ns("devai-sbx-dead", "dead"), _ns(f"devai-sbx-{rec.id}", rec.id)])
    svc._runtime = runtime  # noqa: SLF001 — wired at construction in the app

    assert await svc.reap_orphan_namespaces() == 1
    assert runtime.deleted == ["devai-sbx-dead"]


@pytest.mark.asyncio
async def test_reap_orphan_namespaces_skips_active_terminating_grace() -> None:
    runtime = _NsRuntime([_ns("devai-sbx-gone", "gone", phase="Terminating")])
    svc = _service(runtime=runtime)

    # First sweep: seen once, left to Kubernetes GC.
    assert await svc.reap_orphan_namespaces() == 0
    # Second sweep: still Terminating — re-delete to unstick it.
    assert await svc.reap_orphan_namespaces() == 1
    assert runtime.deleted == ["devai-sbx-gone"]


# ── reference validation ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_rejects_an_agent_that_is_not_published() -> None:
    class _Registry:
        def artifact_exists(self, plural: str, name: str) -> bool:
            return False

    with pytest.raises(SandboxError, match="code-remediator-agent"):
        await _service(registry=_Registry()).create(SandboxSpec.model_validate(_MIN_SPEC), owner="sam@example.com")


@pytest.mark.asyncio
async def test_a_draft_agent_is_not_expected_in_the_catalog() -> None:
    class _Registry:
        def artifact_exists(self, plural: str, name: str) -> bool:
            return False

    spec = SandboxSpec.model_validate(
        {**_MIN_SPEC, "draft": {"kind": "Agent", "metadata": {"name": "draft-x"}, "spec": {"systemPrompt": "hi"}}}
    )

    rec = await _service(registry=_Registry()).create(spec, owner="sam@example.com")
    assert rec.status is SandboxStatus.PENDING


@pytest.mark.asyncio
async def test_unknown_registry_answer_does_not_block_creation() -> None:
    class _Registry:
        def artifact_exists(self, plural: str, name: str) -> None:
            return None  # registry unreachable — degrade, don't block

    rec = await _service(registry=_Registry()).create(SandboxSpec.model_validate(_MIN_SPEC), owner="sam@example.com")
    assert rec.status is SandboxStatus.PENDING


@pytest.mark.asyncio
async def test_a_sandbox_without_an_owner_is_refused() -> None:
    with pytest.raises(SandboxError):
        await _service().create(SandboxSpec.model_validate(_MIN_SPEC), owner="")


def test_a_row_whose_detail_came_back_as_json_text_still_loads() -> None:
    """asyncpg hands JSONB back as text — `detail` needs the same decode `spec` gets.

    Without it a sandbox that recorded a provisioning error is unreadable: every
    read of it 500s, so the UI cannot even show why the sandbox failed.
    """
    now = datetime.now(UTC)
    record = _to_record(
        {
            "id": "f71fa6eb-264f-4556-95e0-aa3ead334471",
            "owner": "sam@example.com",
            "spec": json.dumps(_MIN_SPEC),
            "status": "failed",
            "created_at": now,
            "expires_at": now + timedelta(hours=1),
            "detail": '{"error": "unsupported manifest kind: ConfigMap"}',
        }
    )
    assert record.detail == {"error": "unsupported manifest kind: ConfigMap"}
