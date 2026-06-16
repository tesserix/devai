"""kagent routing in JobRunnerStage.

A pipeline stage routes to a kagent-managed agent over A2A (instead of spawning
a K8s Job) when the agent's registry record carries `devai.io/runtime=kagent`
AND `kagent_url` is configured. Every miss must fall back to the Job path —
kagent is additive and must never be the reason a run fails.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import devai.agentic.kagent_client as kc
from devai.pipeline.interfaces import StageDeps
from devai.pipeline.stages.job_runner import JobRunnerStage
from devai.pipeline.types import DevAITask
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
    task = DevAITask(intent="review PR #5", repo="org/app", triggered_by="alice@x.com", trace_id="t-1")

    result = await _stage(deps).execute(task)

    out = result.data["review_code_output"]
    assert out["runtime"] == "kagent"
    assert out["text"] == "looks good"
    # The hyphenated CR name is used, and identity is forwarded.
    assert _FakeKagentClient.last_call["agent"] == "reviewer-agent"
    assert _FakeKagentClient.last_call["namespace"] == "kagent-system"
    assert _FakeKagentClient.last_call["triggered_by"] == "alice@x.com"
    assert _FakeKagentClient.last_call["trace_id"] == "t-1"


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
async def test_passthrough_falls_back_across_providers(monkeypatch):
    """User's primary provider variant fails (bad model/key) → dispatch falls
    back to the next catalog provider they have a key for."""

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
    result = await _stage(deps).execute(DevAITask(intent="x", triggered_by="alice@x.com"))

    # primary (openai) tried first and failed → fell back to anthropic.
    assert _Failover.calls == ["reviewer-agent-openai", "reviewer-agent-anthropic"]
    assert result.data["review_code_output"]["agent"] == "reviewer-agent-anthropic"


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
        prefs={"kagent_url": "http://my-kagent:8083"},
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
    assert seen["url"] == "http://my-kagent:8083"
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
