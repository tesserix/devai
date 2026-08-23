"""Substrate routing in JobRunnerStage.

A pipeline stage routes to a kagent-managed SandboxAgent over A2A (instead of spawning
a K8s Job) when the agent's registry record carries `devai.io/runtime=kagent`
AND `kagent_url` is configured. Configuration misses and definite pre-connection
rejections fall back to the Job path; possible acceptance must fail closed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

import devai.agentic.kagent_client as kc
from devai.pipeline.interfaces import StageDeps
from devai.pipeline.stages.job_runner import JobRunnerStage
from devai.pipeline.types import DevAITask
from devai.sandbox.models import AgentRef, ModelRef, SandboxRecord, SandboxSpec, SandboxStatus
from devai.settings.models import Connector, Scope


def _agent_meta(name: str, labels: dict[str, str]):
    return SimpleNamespace(
        name=name,
        image="",
        description="",
        version="",
        framework="",
        language="",
        model_provider="",
        model_name="",
        skills=[],
        prompts=[],
        mcp_servers=[],
        labels=labels,
    )


def _deps(
    *,
    kagent_url: str,
    agent_labels: dict[str, str],
    kagent_enabled: bool = True,
    kagent_passthrough: bool = False,
    settings_service=None,
):
    config = SimpleNamespace(
        kagent_url=kagent_url,
        kagent_default_namespace="kagent-system",
        kagent_enabled=kagent_enabled,
        kagent_passthrough=kagent_passthrough,
        kagent_model_provider="anthropic",
        kagent_provider_variants="anthropic,openai",
        kagent_catalog=(
            '[{"suffix":"anthropic","provider":"anthropic","model":"claude-x"},'
            '{"suffix":"openai","provider":"openai","model":"gpt-4.1"},'
            '{"suffix":"openai-o3","provider":"openai","model":"o3"}]'
        ),
        agentgateway_url="",
        auth_bff_shared_secret="",
    )
    registry = SimpleNamespace(get_agent=lambda n: _agent_meta("reviewer-agent", agent_labels))
    # No `k8s_runtime` / `job_watcher` in extra → the Job path stubs out, which
    # lets us prove the kagent path fired by what it returns instead.
    return StageDeps(config=config, extra={"registry_client": registry}, settings_service=settings_service)


class _FakeSettingsService:
    """Minimal SettingsService for build_overlay — returns the given connectors."""

    def __init__(self, connectors: list[Connector], secrets: dict[str, str] | None = None):
        self._connectors = connectors
        self._secrets = secrets or {}

    async def list_connectors(self, scope, scope_id):
        return [c for c in self._connectors if c.scope == scope and c.scope_id == scope_id]

    async def list_user_connectors_by_email(self, email):
        return [c for c in self._connectors if c.scope == Scope.USER and c.scope_id == email]

    async def resolve_secret(self, ref):
        return self._secrets.get(ref)


def _stage(deps) -> JobRunnerStage:
    return JobRunnerStage(deps, {"__stage_name": "review_code"})


class _FakeKagentClient:
    last_call: dict = {}

    async def dispatch(self, agent, message, **kw):
        _FakeKagentClient.last_call = {"agent": agent, "message": message, **kw}
        return {"status": {"message": {"parts": [{"kind": "text", "text": "looks good"}]}}}


@pytest.mark.asyncio
async def test_routes_to_kagent_when_labelled_and_configured(monkeypatch):
    monkeypatch.setattr(kc, "create_kagent_client", lambda settings: _FakeKagentClient())
    deps = _deps(kagent_url="http://kagent:8083", agent_labels={"devai.io/runtime": "kagent"})
    task = DevAITask(id="task-1", intent="review PR #5", repo="org/app", triggered_by="alice@x.com", trace_id="t-1")

    result = await _stage(deps).execute(task)

    out = result.data["review_code_output"]
    assert out["runtime"] == "kagent"
    assert out["execution_target"] == "substrate"
    assert out["text"] == "looks good"
    # The hyphenated CR name is used, and identity is forwarded.
    assert _FakeKagentClient.last_call["agent"] == "reviewer-agent"
    assert _FakeKagentClient.last_call["target"] is kc.KagentDispatchTarget.SANDBOX_AGENT
    assert _FakeKagentClient.last_call["namespace"] == "kagent-system"
    assert _FakeKagentClient.last_call["triggered_by"] == "alice@x.com"
    assert _FakeKagentClient.last_call["trace_id"] == "t-1"
    assert _FakeKagentClient.last_call["request_id"] == "task-1:review_code:reviewer-agent"
    assert _FakeKagentClient.last_call["message_id"] == "task-1:review_code:reviewer-agent"


@pytest.mark.asyncio
async def test_sandboxed_evaluation_never_bypasses_the_job_boundary_through_kagent(monkeypatch):
    _FakeKagentClient.last_call = {}
    monkeypatch.setattr(kc, "create_kagent_client", lambda settings: _FakeKagentClient())
    deps = _deps(kagent_url="http://kagent:8083", agent_labels={"devai.io/runtime": "kagent"})
    now = datetime.now(UTC)
    record = SandboxRecord(
        id="sb-1",
        owner="tenant-a:alice",
        spec=SandboxSpec(
            agent=AgentRef(name="reviewer-agent", version="7"),
            model=ModelRef(provider="anthropic", model="claude-sonnet-4"),
        ),
        status=SandboxStatus.READY,
        created_at=now,
        expires_at=now + timedelta(hours=1),
    )
    stage = JobRunnerStage(
        deps,
        {"__stage_name": "evaluation", "agent": "reviewer-agent", "__sandbox": record.model_dump(mode="json")},
    )

    result = await stage.execute(DevAITask(intent="evaluate", triggered_by="alice@x.com"))

    assert _FakeKagentClient.last_call == {}
    assert "K8s runtime not available" in result.message


@pytest.mark.asyncio
async def test_passthrough_forwards_user_own_key(monkeypatch):
    """With passthrough on, the triggering user's OWN Anthropic key (their
    connector, resolved via the overlay) is forwarded as the A2A Bearer token."""
    monkeypatch.setattr(kc, "create_kagent_client", lambda settings: _FakeKagentClient())
    user_llm = Connector(
        scope=Scope.USER,
        scope_id="alice@x.com",
        connector_key="llm",
        provider="anthropic",
        secret_refs={"anthropic_api_key": "ref-anthropic"},
        enabled=True,
    )
    svc = _FakeSettingsService([user_llm], secrets={"ref-anthropic": "sk-ant-alice-key"})
    deps = _deps(
        kagent_url="http://kagent:8083",
        agent_labels={"devai.io/runtime": "kagent"},
        kagent_passthrough=True,
        settings_service=svc,
    )
    result = await _stage(deps).execute(DevAITask(intent="x", triggered_by="alice@x.com"))

    assert result.data["review_code_output"]["runtime"] == "kagent"
    assert _FakeKagentClient.last_call["api_key"] == "sk-ant-alice-key"


@pytest.mark.asyncio
async def test_passthrough_no_user_key_falls_back_to_job(monkeypatch):
    """Passthrough on but the user has no own key for the kagent provider →
    Job path (never bill a kagent run to the platform key under passthrough)."""

    class _NoDispatch:
        async def dispatch(self, *a, **k):
            raise AssertionError("must not dispatch to kagent without the user's own key")

    monkeypatch.setattr(kc, "create_kagent_client", lambda settings: _NoDispatch())
    deps = _deps(
        kagent_url="http://kagent:8083",
        agent_labels={"devai.io/runtime": "kagent"},
        kagent_passthrough=True,
        settings_service=_FakeSettingsService([]),  # no connectors → no user key
    )
    result = await _stage(deps).execute(DevAITask(intent="x", triggered_by="bob@x.com"))
    assert result.data.get("review_code_stub") is True


@pytest.mark.asyncio
async def test_passthrough_never_forwards_a_shared_tenant_key(monkeypatch):
    """A tenant connector can configure the normal Job path, but it is not the
    triggering user's own credential and must never cross the A2A boundary."""

    class _NoDispatch:
        async def dispatch(self, *a, **k):
            raise AssertionError("must not forward a shared tenant key to kagent")

    monkeypatch.setattr(kc, "create_kagent_client", lambda settings: _NoDispatch())
    shared_llm = Connector(
        scope=Scope.TENANT,
        scope_id="tenant-a",
        connector_key="llm",
        provider="anthropic",
        secret_refs={"anthropic_api_key": "ref-tenant-anthropic"},
        enabled=True,
    )
    svc = _FakeSettingsService([shared_llm], secrets={"ref-tenant-anthropic": "sk-ant-shared-tenant"})
    deps = _deps(
        kagent_url="http://kagent:8083",
        agent_labels={"devai.io/runtime": "kagent"},
        kagent_passthrough=True,
        settings_service=svc,
    )
    task = DevAITask(
        intent="x",
        triggered_by="alice@x.com",
        principal={"email": "alice@x.com", "uid": "alice", "tenant_id": "tenant-a"},
    )

    result = await _stage(deps).execute(task)

    assert result.data.get("review_code_stub") is True


@pytest.mark.asyncio
async def test_passthrough_per_model_variant(monkeypatch):
    """User picks a specific model (openai o3) → dispatch targets the per-model
    variant `<agent>-openai-o3`, not the provider default `<agent>-openai`."""
    monkeypatch.setattr(kc, "create_kagent_client", lambda settings: _FakeKagentClient())
    conn = Connector(
        scope=Scope.USER,
        scope_id="alice@x.com",
        connector_key="llm",
        provider="openai",
        prefs={"openai_model": "o3"},
        secret_refs={"openai_api_key": "ref-o"},
        enabled=True,
    )
    svc = _FakeSettingsService([conn], secrets={"ref-o": "sk-openai-alice"})
    deps = _deps(
        kagent_url="http://kagent:8083",
        agent_labels={"devai.io/runtime": "kagent"},
        kagent_passthrough=True,
        settings_service=svc,
    )
    result = await _stage(deps).execute(DevAITask(intent="x", triggered_by="alice@x.com"))

    assert _FakeKagentClient.last_call["agent"] == "reviewer-agent-openai-o3"
    assert _FakeKagentClient.last_call["api_key"] == "sk-openai-alice"
    assert result.data["review_code_output"]["agent"] == "reviewer-agent-openai-o3"


@pytest.mark.asyncio
async def test_passthrough_does_not_replay_an_accepted_failure(monkeypatch):
    """A failed A2A task was accepted and may have run tools, so it is not replayed."""

    class _Failover:
        calls: list = []

        async def dispatch(self, agent, message, **kw):
            _Failover.calls.append(agent)
            if agent.endswith("-openai"):
                return {"status": {"state": "failed", "message": {"parts": [{"kind": "text", "text": "404 model"}]}}}
            return {"status": {"state": "completed", "message": {"parts": [{"kind": "text", "text": "ok"}]}}}

    _Failover.calls = []
    monkeypatch.setattr(kc, "create_kagent_client", lambda settings: _Failover())
    # One LLM connector, provider=openai, holding BOTH provider keys.
    conn = Connector(
        scope=Scope.USER,
        scope_id="alice@x.com",
        connector_key="llm",
        provider="openai",
        secret_refs={"openai_api_key": "ref-o", "anthropic_api_key": "ref-a"},
        enabled=True,
    )
    svc = _FakeSettingsService([conn], secrets={"ref-o": "sk-openai", "ref-a": "sk-ant"})
    deps = _deps(
        kagent_url="http://kagent:8083",
        agent_labels={"devai.io/runtime": "kagent"},
        kagent_passthrough=True,
        settings_service=svc,
    )
    with pytest.raises(kc.KagentDispatchOutcomeUncertain):
        await _stage(deps).execute(DevAITask(intent="x", triggered_by="alice@x.com"))

    assert _Failover.calls == ["reviewer-agent-openai"]


@pytest.mark.asyncio
async def test_shared_key_mode_does_not_fall_back_after_accepted_failure(monkeypatch):
    class _FailedTask:
        async def dispatch(self, *a, **k):
            return {"status": {"state": "failed"}}

    monkeypatch.setattr(kc, "create_kagent_client", lambda settings: _FailedTask())
    deps = _deps(kagent_url="http://kagent:8083", agent_labels={"devai.io/runtime": "kagent"})

    with pytest.raises(kc.KagentDispatchOutcomeUncertain):
        await _stage(deps).execute(DevAITask(intent="x"))


@pytest.mark.asyncio
async def test_passthrough_skips_non_human_principal(monkeypatch):
    """Isolation guard: a system/webhook principal never forwards a key, even if
    a connector resolves for it — passthrough is HUMAN-only; it falls back to Job."""

    class _NoDispatch:
        async def dispatch(self, *a, **k):
            raise AssertionError("must not passthrough for a non-human principal")

    monkeypatch.setattr(kc, "create_kagent_client", lambda settings: _NoDispatch())
    sys_conn = Connector(
        scope=Scope.USER,
        scope_id="system:cron",
        connector_key="llm",
        provider="anthropic",
        secret_refs={"anthropic_api_key": "ref-sys"},
        enabled=True,
    )
    svc = _FakeSettingsService([sys_conn], secrets={"ref-sys": "sk-ant-service-key"})
    deps = _deps(
        kagent_url="http://kagent:8083",
        agent_labels={"devai.io/runtime": "kagent"},
        kagent_passthrough=True,
        settings_service=svc,
    )
    result = await _stage(deps).execute(DevAITask(intent="x", triggered_by="system:cron"))
    assert result.data.get("review_code_stub") is True


@pytest.mark.asyncio
async def test_falls_back_to_job_when_not_labelled(monkeypatch):
    # A client that would explode if called — it must NOT be.
    def _boom(_settings):
        raise AssertionError("kagent client must not be built for a non-kagent agent")

    monkeypatch.setattr(kc, "create_kagent_client", _boom)
    deps = _deps(kagent_url="http://kagent:8083", agent_labels={})  # no runtime label
    result = await _stage(deps).execute(DevAITask(intent="x"))

    # Job runtime absent in extra → stub (the inline-fallback path), proving we
    # did not take the kagent branch.
    assert result.data.get("review_code_stub") is True


@pytest.mark.asyncio
async def test_falls_back_to_job_when_kagent_url_unset(monkeypatch):
    monkeypatch.setattr(kc, "create_kagent_client", lambda settings: None)
    deps = _deps(kagent_url="", agent_labels={"devai.io/runtime": "kagent"})
    result = await _stage(deps).execute(DevAITask(intent="x"))
    assert result.data.get("review_code_stub") is True


@pytest.mark.asyncio
async def test_settings_switch_off_falls_back_to_job(monkeypatch):
    """A GLOBAL `kagent` connector set to `off` disables kagent dynamically —
    the labelled agent runs as a Job, no restart, resolved through the real
    overlay + on/off→bool coercion."""

    def _boom(_settings):
        raise AssertionError("kagent must not be dispatched when switched off")

    monkeypatch.setattr(kc, "create_kagent_client", _boom)
    off = Connector(scope=Scope.GLOBAL, scope_id="", connector_key="kagent", provider="off", enabled=True)
    deps = _deps(
        kagent_url="http://kagent:8083",
        agent_labels={"devai.io/runtime": "kagent"},
        settings_service=_FakeSettingsService([off]),
    )
    result = await _stage(deps).execute(DevAITask(intent="x", triggered_by="alice@x.com"))
    assert result.data.get("review_code_stub") is True


@pytest.mark.asyncio
async def test_settings_switch_on_with_per_user_url(monkeypatch):
    """A USER `kagent` connector set to `on` with a custom URL routes to THAT
    controller — proving the per-principal overlay flows into the client."""
    seen: dict = {}

    def _capture(settings):
        seen["url"] = getattr(settings, "kagent_url", None)
        seen["enabled"] = getattr(settings, "kagent_enabled", None)
        return _FakeKagentClient()

    monkeypatch.setattr(kc, "create_kagent_client", _capture)
    on = Connector(
        scope=Scope.USER,
        scope_id="alice@x.com",
        connector_key="kagent",
        provider="on",
        prefs={"kagent_url": "https://kagent.example.com"},
        enabled=True,
    )
    deps = _deps(
        kagent_url="",  # platform default empty — the user's own URL must win
        agent_labels={"devai.io/runtime": "kagent"},
        kagent_enabled=False,  # base off — the user's `on` connector must win
        settings_service=_FakeSettingsService([on]),
    )
    result = await _stage(deps).execute(DevAITask(intent="x", triggered_by="alice@x.com"))

    assert result.data["review_code_output"]["runtime"] == "kagent"
    assert seen["url"] == "https://kagent.example.com"
    assert seen["enabled"] is True


@pytest.mark.asyncio
async def test_falls_back_to_job_on_dispatch_error(monkeypatch):
    class _Failing:
        async def dispatch(self, *a, **k):
            raise kc.KagentError("boom")

    monkeypatch.setattr(kc, "create_kagent_client", lambda settings: _Failing())
    deps = _deps(kagent_url="http://kagent:8083", agent_labels={"devai.io/runtime": "kagent"})
    result = await _stage(deps).execute(DevAITask(intent="x"))
    # Dispatch failed → fall through to the Job path (stub, since no runtime).
    assert result.data.get("review_code_stub") is True


@pytest.mark.asyncio
async def test_does_not_fall_back_when_dispatch_outcome_is_uncertain(monkeypatch):
    class _Uncertain:
        async def dispatch(self, *a, **k):
            raise kc.KagentDispatchOutcomeUncertain("response lost")

    monkeypatch.setattr(kc, "create_kagent_client", lambda settings: _Uncertain())
    deps = _deps(kagent_url="http://kagent:8083", agent_labels={"devai.io/runtime": "kagent"})

    with pytest.raises(kc.KagentDispatchOutcomeUncertain):
        await _stage(deps).execute(DevAITask(intent="x"))


@pytest.mark.asyncio
async def test_falls_back_to_job_on_non_object_dispatch_response(monkeypatch):
    class _InvalidResponse:
        async def dispatch(self, *a, **k):
            return "unexpected response"

    monkeypatch.setattr(kc, "create_kagent_client", lambda settings: _InvalidResponse())
    deps = _deps(kagent_url="http://kagent:8083", agent_labels={"devai.io/runtime": "kagent"})

    result = await _stage(deps).execute(DevAITask(intent="x"))

    assert result.data.get("review_code_stub") is True
