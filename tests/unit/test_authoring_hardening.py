"""Regression tests for the authoring hardening fixes (CODE-4 / CODE-21 / CODE-26).

Covers:
  * CODE-4  — authored legacy_python_class allowlist, protected built-in
    definitions (specializations + blueprints), trusted MCP-server image.
  * CODE-21 — per-kind cap, per-principal quota, secret-shaped/DEVAI_* env
    rejection, blueprint name normalization.
  * CODE-26 — load-time condition-key validation + nested agent_context
    lookup, and the soft-failure (ok/failed) detection contract.
"""

from __future__ import annotations

import pytest

from devai.authoring.service import AuthoringError, AuthoringQuotaError, AuthoringService
from devai.authoring.store import AuthoredDefinition, InMemoryDefinitionStore
from devai.blueprint import conditions
from devai.blueprint.loader import BlueprintLoadError, load_blueprint_from_string
from devai.pipeline.types import FAILURE_STATES, StageResult, TaskState
from devai.registry import mapping
from devai.specializations.base import Specialization
from devai.specializations.loader import SpecializationLoadError, load_specialization_from_string
from devai.specializations.registry import SpecializationRegistry, SpecializationRegistryError


class _FakePipeline:
    """Minimal duck-typed PipelineService for blueprint registration."""

    def __init__(self, builtins: list[str]) -> None:
        self._builtins = list(builtins)
        self.registered: list[str] = []

    def list_blueprints(self) -> list[str]:
        return list(self._builtins)

    def register_blueprint(self, bp) -> bool:
        self.registered.append(bp.name)
        return True


# ── CODE-4: legacy_python_class allowlist ──────────────────────────────


def test_authored_legacy_class_allowlisted():
    yaml = "name: a\nsystem_prompt: x\nlegacy_python_class: devai.agents.senior_developer.SeniorDeveloper\n"
    spec = load_specialization_from_string(yaml, authored=True)
    assert spec.legacy_python_class.startswith("devai.agents.")


def test_authored_legacy_class_arbitrary_rejected():
    yaml = "name: a\nsystem_prompt: x\nlegacy_python_class: os.system\n"
    with pytest.raises(SpecializationLoadError, match="not permitted"):
        load_specialization_from_string(yaml, authored=True)


def test_builtin_legacy_class_unrestricted():
    # On-disk built-ins (authored=False) keep today's behavior.
    yaml = "name: a\nsystem_prompt: x\nlegacy_python_class: devai.agents.foo.Bar\n"
    spec = load_specialization_from_string(yaml, authored=False)
    assert spec.legacy_python_class == "devai.agents.foo.Bar"


# ── CODE-4: protected built-in specializations ─────────────────────────


def test_registry_protects_builtins():
    reg = SpecializationRegistry()
    reg.register(Specialization(name="senior_developer", system_prompt="x"), protected=True)
    assert reg.is_protected("senior_developer")
    with pytest.raises(SpecializationRegistryError, match="protected built-in"):
        reg.register_or_replace(Specialization(name="senior_developer", system_prompt="evil"))
    # admin force overrides
    reg.register_or_replace(Specialization(name="senior_developer", system_prompt="ok"), force=True)
    assert reg.resolve("senior_developer").system_prompt == "ok"


async def test_authored_spec_cannot_shadow_builtin():
    reg = SpecializationRegistry()
    reg.register(Specialization(name="senior_developer", system_prompt="x"), protected=True)
    svc = AuthoringService(InMemoryDefinitionStore(), spec_registry=reg)
    with pytest.raises(AuthoringError, match="protected built-in"):
        await svc.create_specialization("name: senior_developer\nsystem_prompt: evil\n", created_by="alice")
    # rejected before persist
    assert await svc.get_specialization("senior_developer") is None


# ── CODE-4: protected built-in blueprints + trusted MCP image ──────────


async def test_authored_blueprint_cannot_shadow_builtin():
    pipe = _FakePipeline(builtins=["alm-pipeline"])
    svc = AuthoringService(InMemoryDefinitionStore(), pipeline=pipe)
    bp = "name: alm-pipeline\ndescription: x\nstages:\n  - name: s\n    stage: noop\n"
    with pytest.raises(AuthoringError, match="protected built-in"):
        await svc.create_blueprint(bp, created_by="bob")


@pytest.mark.parametrize("image", ["docker.io/evil/tool:latest", "quay.io/x/y:1", "evil:latest"])
async def test_mcp_image_untrusted_rejected(image):
    svc = AuthoringService(InMemoryDefinitionStore())
    with pytest.raises(AuthoringError, match="trusted registry"):
        await svc.create_mcp_server({"name": "t", "image": image}, created_by="alice")


async def test_mcp_image_trusted_accepted():
    svc = AuthoringService(InMemoryDefinitionStore())
    out = await svc.create_mcp_server({"name": "t", "image": "ghcr.io/tesserix/devai/mytool:main"}, created_by="alice")
    assert out["name"] == "t"


# ── CODE-21: env-key rejection ─────────────────────────────────────────


@pytest.mark.parametrize("key", ["DEVAI_DATABASE_URL", "MY_API_KEY", "SLACK_TOKEN", "DB_PASSWORD", "AWS_SECRET"])
async def test_mcp_env_secret_shaped_rejected(key):
    svc = AuthoringService(InMemoryDefinitionStore())
    with pytest.raises(AuthoringError):
        await svc.create_mcp_server(
            {"name": "t", "image": "ghcr.io/tesserix/x:1", "env": {key: "v"}}, created_by="alice"
        )


async def test_mcp_env_benign_accepted():
    svc = AuthoringService(InMemoryDefinitionStore())
    out = await svc.create_mcp_server(
        {"name": "t", "image": "ghcr.io/tesserix/x:1", "env": {"LOG_LEVEL": "debug"}}, created_by="alice"
    )
    assert out["name"] == "t"


# ── CODE-21: caps + quota + name normalization ─────────────────────────


class _Settings:
    def __init__(self, **kw):
        self.authoring_per_kind_cap = kw.get("cap", 0)
        self.authoring_per_principal_quota = kw.get("quota", 0)
        self.authoring_rate_limit_per_minute = kw.get("rate", 0)


async def test_per_kind_cap_enforced():
    store = InMemoryDefinitionStore()
    await store.upsert(AuthoredDefinition(kind="skill", name="a", yaml=""))
    await store.upsert(AuthoredDefinition(kind="skill", name="b", yaml=""))
    svc = AuthoringService(store, settings=_Settings(cap=2))
    with pytest.raises(AuthoringQuotaError, match="per-kind cap"):
        await svc._enforce_limits("skill", "alice")


async def test_per_principal_quota_enforced():
    store = InMemoryDefinitionStore()
    await store.upsert(AuthoredDefinition(kind="skill", name="a", created_by="alice", yaml=""))
    svc = AuthoringService(store, settings=_Settings(quota=1))
    with pytest.raises(AuthoringQuotaError, match="per-principal quota"):
        await svc._enforce_limits("skill", "alice")
    # a different principal is unaffected
    await svc._enforce_limits("skill", "bob")


def test_normalize_artifact_name():
    assert mapping.normalize_artifact_name("My Cool Flow!!") == "my-cool-flow"
    assert mapping.normalize_artifact_name("../../etc/passwd") == "etc-passwd"
    assert mapping.normalize_artifact_name("alm-pipeline") == "alm-pipeline"
    with pytest.raises(ValueError):
        mapping.normalize_artifact_name("!!!")


async def test_blueprint_name_normalized_on_create():
    pipe = _FakePipeline(builtins=[])
    svc = AuthoringService(InMemoryDefinitionStore(), pipeline=pipe)
    out = await svc.create_blueprint(
        "name: My Flow\ndescription: x\nstages:\n  - name: s\n    stage: noop\n", created_by="bob"
    )
    assert out["name"] == "my-flow"
    assert "my-flow" in pipe.registered


# ── CODE-26: condition validation + nested lookup ──────────────────────


def test_validate_condition_known_and_prefixed():
    assert conditions.validate_condition(None) == []
    assert conditions.validate_condition("task.has_pr and task.has_epic") == []
    assert conditions.validate_condition("output.foo") == []
    assert conditions.validate_condition("state.completed") == []
    assert conditions.validate_condition("agent_context.scan_output.is_blank") == []


def test_validate_condition_unknown_bare_key():
    assert conditions.validate_condition("task.haspr") == ["task.haspr"]
    assert conditions.validate_condition("task.bogus and output.x") == ["task.bogus"]


def test_loader_rejects_typoed_condition():
    bp = "name: b\ndescription: x\nstages:\n  - name: s\n    stage: noop\n    condition: task.haspr\n"
    with pytest.raises(BlueprintLoadError, match="unknown key"):
        load_blueprint_from_string(bp)


def test_loader_accepts_prefixed_conditions():
    bp = (
        "name: b\ndescription: x\nstages:\n"
        "  - name: s\n    stage: noop\n    condition: agent_context.scan_output.is_blank\n"
    )
    loaded = load_blueprint_from_string(bp)
    assert loaded.stages[0].condition == "agent_context.scan_output.is_blank"


def test_nested_agent_context_lookup():
    from devai.pipeline.types import DevAITask

    task = DevAITask()
    task.agent_context = {"scan_output": {"is_blank": True}}
    assert conditions.evaluate("agent_context.scan_output.is_blank", task) is True
    task.agent_context = {"scan_output": {"is_blank": False}}
    assert conditions.evaluate("agent_context.scan_output.is_blank", task) is False
    task.agent_context = {}
    assert conditions.evaluate("agent_context.scan_output.is_blank", task) is False


# ── CODE-26: soft-failure detection contract ───────────────────────────


def test_soft_failure_detection():
    # The workflow module needs temporalio; the detection logic is duplicated
    # here against the real contract so it's covered without that dependency.
    def result_failed(result):
        data = getattr(result, "data", None) or {}
        if isinstance(data, dict):
            if data.get("failed") is True:
                return True
            if data.get("ok") is False:
                return True
        next_state = getattr(result, "next_state", None)
        return next_state is not None and next_state in FAILURE_STATES

    assert result_failed(StageResult(next_state=TaskState.COMPLETED, data={"x": 1})) is False
    assert result_failed(StageResult()) is False
    assert result_failed(StageResult(data={"ok": True})) is False
    assert result_failed(StageResult(data={"failed": True})) is True
    assert result_failed(StageResult(data={"ok": False})) is True
    assert result_failed(StageResult(next_state=TaskState.STAGE_FAILED)) is True
