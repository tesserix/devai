"""Running an agent inside its pinned sandbox and getting a trace back.

This is the loop the platform exists for: author an agent, invoke it under a
pinned configuration, read what it actually did.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from devai.adapters.llm.base import LLMAdapter, LLMResponse, LLMUsage, ToolCall
from devai.config import Settings
from devai.pipeline.interfaces import StageDeps
from devai.sandbox.credentials import SandboxMeteredLLMAdapter
from devai.sandbox.invoke import SandboxInvoker
from devai.sandbox.models import (
    AgentRef,
    ImportSnapshot,
    ModelRef,
    SandboxLimits,
    SandboxRecord,
    SandboxSpec,
    SandboxStatus,
    ToolMode,
    ToolPolicy,
)
from devai.sandbox.portable_client import PortableAgentResult
from devai.sandbox.trace import TraceStore
from devai.specializations.loader import load_specialization_from_string
from devai.specializations.registry import SpecializationRegistry

_SPEC_YAML = """
name: release_notes_writer
display_name: Release Notes Writer
llm_provider: claude
allowed_tools:
  - scm_list_files
  - scm_create_pr
system_prompt: |
  You write release notes.
"""


class _ScriptedLLM(LLMAdapter):
    provider_name = "scripted"

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.calls = 0

    async def generate(self, request):  # type: ignore[override]
        self.calls += 1
        return self._responses.pop(0)


def _record(**kw) -> SandboxRecord:
    now = datetime.now(UTC)
    spec = SandboxSpec(
        agent=AgentRef(name=kw.get("agent", "release-notes-writer"), version="v1"),
        model=ModelRef(provider="anthropic", model="claude-sonnet-4-20250514"),
        tools=kw.get("tools", ToolPolicy(default_mode=ToolMode.MOCK)),
        draft=kw.get("draft"),
        adk_version="0.2.0",
        limits=kw.get("limits", SandboxLimits()),
    )
    return SandboxRecord(
        id=kw.get("id", "sb-1"),
        owner="sam@example.com",
        spec=spec,
        status=kw.get("status", SandboxStatus.READY),
        created_at=now,
        expires_at=now + timedelta(hours=1),
    )


class _Specs:
    """Stands in for SpecializationService — the invoker only resolves roles."""

    def __init__(self, registry: SpecializationRegistry) -> None:
        self._registry = registry

    async def resolve_runnable(self, name: str):
        return self._registry.resolve(name) if self._registry.has(name) else None


def _registry() -> SpecializationRegistry:
    reg = SpecializationRegistry()
    reg.register(load_specialization_from_string(_SPEC_YAML))
    return reg


class _GrantedSandboxLLM:
    def __init__(self, llm: LLMAdapter) -> None:
        self._llm = llm

    async def resolve(self, record, deps):
        del record
        return replace(
            deps,
            llm=self._llm,
            scm=None,
            memory=None,
            secrets=None,
            settings_service=None,
            llm_resolver=None,
            scm_resolver=None,
            extra=None,
        )


def _invoker(llm, *, registry=None, store=None, telemetry=None, portable_client=None, catalog=None) -> SandboxInvoker:
    return SandboxInvoker(
        specializations=registry if registry is not None else _Specs(_registry()),
        deps=StageDeps(config=Settings(), llm=llm),
        traces=store or TraceStore(None),
        credentials=_GrantedSandboxLLM(llm),  # type: ignore[arg-type]
        telemetry=telemetry,
        portable_client=portable_client,
        registry=catalog,
    )


def _imported_record() -> SandboxRecord:
    record = _record(agent="support")
    snapshot = ImportSnapshot(
        import_id="bf2ef27d-98a2-4ce4-b87a-c6952d2d5d09",
        registry_ref="registry://acme/agents/acme/support@1.4.0",
        agent_digest="sha256:" + "a" * 64,
        dependency_lock=[],
        runtime={"type": "remote", "protocol": "a2a", "url": "https://agent.acme.example/a2a/v1"},
        permissions={},
    )
    spec = record.spec.model_copy(
        update={
            "agent": AgentRef(name="support", version="1.4.0"),
            "import_id": snapshot.import_id,
            "import_snapshot": snapshot,
        }
    )
    return record.model_copy(update={"spec": spec})


async def test_one_turn_produces_a_trace_with_the_final_answer() -> None:
    llm = _ScriptedLLM([LLMResponse(text="Here are the notes.")])

    inv = await _invoker(llm).invoke(_record(), message="summarise the diff", triggered_by="sam@example.com")

    assert inv.ok
    assert inv.final_text == "Here are the notes."
    assert inv.agent == "release_notes_writer"
    assert [s.kind for s in inv.steps][0] == "prompt"
    assert [s.kind for s in inv.steps][-1] == "response"


async def test_imported_agent_uses_its_portable_runtime_and_preserves_trace_evidence() -> None:
    class _PortableClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        async def invoke(self, record, *, message: str, triggered_by: str):
            self.calls.append((message, triggered_by))
            return PortableAgentResult(
                final_text="portable answer",
                backend="remote:a2a",
                raw={"kind": "message"},
            )

    portable = _PortableClient()
    invocation = await _invoker(
        _ScriptedLLM([]),
        portable_client=portable,
    ).invoke(_imported_record(), message="triage this", triggered_by="tenant-a:alice")

    assert portable.calls == [("triage this", "tenant-a:alice")]
    assert invocation.ok is True
    assert invocation.final_text == "portable answer"
    assert invocation.execution_backend == "remote:a2a"
    assert [step.kind for step in invocation.steps] == ["prompt", "response"]


async def test_the_llm_step_carries_the_tokens_the_metrics_are_built_from() -> None:
    llm = _ScriptedLLM([LLMResponse(text="done", usage=LLMUsage(prompt_tokens=120, completion_tokens=40))])

    inv = await _invoker(llm).invoke(_record(), message="go", triggered_by="sam@example.com")

    assert inv.totals["total_tokens"] == 160
    assert inv.totals["llm_calls"] == 1
    assert inv.totals["cost_usd"] == pytest.approx(0.00096)


async def test_a_tool_call_is_recorded_with_the_mode_that_served_it() -> None:
    llm = _ScriptedLLM(
        [
            LLMResponse(tool_calls=[ToolCall(id="c1", name="scm_list_files", arguments={})]),
            LLMResponse(text="done"),
        ]
    )

    inv = await _invoker(llm).invoke(_record(), message="go", triggered_by="sam@example.com")

    tool_steps = [s for s in inv.steps if s.kind == "tool"]
    assert [s.name for s in tool_steps] == ["scm_list_files"]
    assert tool_steps[0].mode == "mock"
    assert inv.totals["tool_calls"] == 1


async def test_each_llm_call_is_traced_in_causal_order_with_attribution() -> None:
    llm = _ScriptedLLM(
        [
            LLMResponse(
                text="I will inspect the files.",
                tool_calls=[ToolCall(id="c1", name="scm_list_files", arguments={})],
                usage=LLMUsage(prompt_tokens=12, completion_tokens=4),
                model="claude-sonnet-4-20250514",
                provider="anthropic",
                latency_ms=17,
            ),
            LLMResponse(
                text="done",
                usage=LLMUsage(prompt_tokens=20, completion_tokens=3),
                model="claude-sonnet-4-20250514",
                provider="anthropic",
                latency_ms=11,
            ),
        ]
    )

    inv = await _invoker(llm).invoke(_record(), message="go", triggered_by="sam@example.com")

    assert [step.kind for step in inv.steps] == ["prompt", "prompt", "llm", "tool", "llm", "response"]
    llm_steps = [step for step in inv.steps if step.kind == "llm"]
    assert [step.output for step in llm_steps] == ["I will inspect the files.", "done"]
    assert [(step.provider, step.name, step.prompt_version) for step in llm_steps] == [
        ("anthropic", "claude-sonnet-4-20250514", "v1"),
        ("anthropic", "claude-sonnet-4-20250514", "v1"),
    ]
    assert [(step.prompt_tokens, step.completion_tokens) for step in llm_steps] == [(12, 4), (20, 3)]
    assert [step.latency_ms for step in llm_steps] == [17, 11]
    assert all(step.cost_usd > 0 for step in llm_steps)


async def test_provider_attribution_comes_from_the_immutable_sandbox_pin() -> None:
    llm = _ScriptedLLM(
        [
            LLMResponse(
                text="done",
                provider="inconsistent-upstream-label",
                model="claude-sonnet-4-20250514",
            )
        ]
    )

    invocation = await _invoker(llm).invoke(
        _record(),
        message="go",
        triggered_by="sam@example.com",
    )

    assert next(step for step in invocation.steps if step.kind == "llm").provider == "anthropic"


async def test_the_full_trace_spine_is_mirrored_to_telemetry_without_payloads() -> None:
    class _Telemetry:
        def __init__(self) -> None:
            self.spans: list[tuple[str, dict[str, object]]] = []

        @contextmanager
        def span(self, name, *, attributes=None):
            self.spans.append((name, dict(attributes or {})))
            yield None

    telemetry = _Telemetry()
    secret = "sk-ant-api03-never-export-this"
    llm = _ScriptedLLM(
        [
            LLMResponse(tool_calls=[ToolCall(id="c1", name="scm_list_files", arguments={})]),
            LLMResponse(text=f"done without echoing {secret}"),
        ]
    )

    await _invoker(llm, telemetry=telemetry).invoke(
        _record(),
        message=f"inspect; credential={secret}",
        triggered_by="sam@example.com",
    )

    names = [name for name, _ in telemetry.spans]
    assert names == [
        "sandbox.invocation",
        "sandbox.prompt",
        "sandbox.prompt",
        "sandbox.llm",
        "sandbox.tool",
        "sandbox.llm",
        "sandbox.response",
    ]
    llm_attrs = [attrs for name, attrs in telemetry.spans if name == "sandbox.llm"]
    assert all(attrs["provider"] == "anthropic" for attrs in llm_attrs)
    assert all(attrs["prompt_version"] == "v1" for attrs in llm_attrs)
    assert secret not in repr(telemetry.spans)


async def test_a_side_effecting_tool_is_blocked_by_the_pinned_policy() -> None:
    llm = _ScriptedLLM(
        [
            LLMResponse(tool_calls=[ToolCall(id="c1", name="scm_create_pr", arguments={})]),
            LLMResponse(text="ok"),
        ]
    )
    record = _record(tools=ToolPolicy(default_mode=ToolMode.BLOCK))

    inv = await _invoker(llm).invoke(record, message="ship it", triggered_by="sam@example.com")

    assert inv.totals["blocked_tool_calls"] == 1


async def test_a_tool_outside_the_role_allowlist_never_reaches_the_gateway() -> None:
    # Defence in depth: the role's own allowlist denies first, so the gateway
    # only ever arbitrates tools the agent was entitled to ask for.
    llm = _ScriptedLLM(
        [
            LLMResponse(tool_calls=[ToolCall(id="c1", name="shell_exec", arguments={})]),
            LLMResponse(text="ok"),
        ]
    )

    inv = await _invoker(llm).invoke(_record(), message="go", triggered_by="sam@example.com")

    assert inv.totals["tool_calls"] == 0


async def test_the_trace_is_retrievable_afterwards() -> None:
    store = TraceStore(None)
    inv = await _invoker(_ScriptedLLM([LLMResponse(text="hi")]), store=store).invoke(
        _record(), message="go", triggered_by="sam@example.com"
    )

    assert (await store.get("sb-1", inv.id)) is not None
    assert [i.id for i in await store.list_for_sandbox("sb-1")] == [inv.id]


async def test_an_unknown_agent_is_refused_before_any_model_call() -> None:
    llm = _ScriptedLLM([])

    with pytest.raises(ValueError, match="nope"):
        await _invoker(llm).invoke(_record(agent="nope"), message="go", triggered_by="sam@example.com")

    assert llm.calls == 0


async def test_a_sandbox_that_is_not_live_cannot_be_invoked() -> None:
    llm = _ScriptedLLM([])

    with pytest.raises(ValueError, match="destroyed"):
        await _invoker(llm).invoke(
            _record(status=SandboxStatus.DESTROYED), message="go", triggered_by="sam@example.com"
        )

    assert llm.calls == 0


async def test_a_model_failure_is_a_failed_trace_not_a_lost_one() -> None:
    class _Boom(LLMAdapter):
        provider_name = "boom"

        async def generate(self, request):  # type: ignore[override]
            raise RuntimeError("upstream 503")

    store = TraceStore(None)
    inv = await _invoker(_Boom(), store=store).invoke(_record(), message="go", triggered_by="sam@example.com")

    assert not inv.ok
    assert "503" in inv.error
    assert (await store.get("sb-1", inv.id)) is not None


async def test_wall_clock_budget_stops_the_run_and_records_the_reason(monkeypatch) -> None:
    limits = SandboxLimits.model_construct(max_tokens=100, max_cost_usd=1.0, max_wall_clock_s=0)
    invoker = _invoker(_ScriptedLLM([]))

    async def never_finishes(*args, **kwargs):
        del args, kwargs
        await __import__("asyncio").Event().wait()

    monkeypatch.setattr(invoker, "_run", never_finishes)

    invocation = await invoker.invoke(
        _record(limits=limits),
        message="go",
        triggered_by="sam@example.com",
    )

    assert not invocation.ok
    assert "wall-clock budget" in invocation.error
    assert invocation.steps[-1].error == invocation.error


async def test_a_budget_crossing_keeps_the_call_usage_and_reason_in_the_trace() -> None:
    inner = _ScriptedLLM(
        [
            LLMResponse(
                text="too expensive",
                usage=LLMUsage(prompt_tokens=8, completion_tokens=2, total_tokens=10),
                model="claude-sonnet-4-20250514",
                provider="anthropic",
            )
        ]
    )
    metered = SandboxMeteredLLMAdapter(
        inner,
        sandbox_id="sb-1",
        owner="sam@example.com",
        max_tokens=100,
        max_cost_usd=0.1,
        estimate_cost=lambda provider, model, prompt, completion: 0.2,
    )

    invocation = await _invoker(metered).invoke(
        _record(),
        message="go",
        triggered_by="sam@example.com",
    )

    llm_step = next(step for step in invocation.steps if step.kind == "llm")
    assert not invocation.ok
    assert "cost budget" in invocation.error
    assert llm_step.prompt_tokens == 8
    assert llm_step.completion_tokens == 2
    assert llm_step.cost_usd > 0
    assert "cost budget" in llm_step.error


async def test_provider_errors_are_redacted_before_logging_or_tracing(caplog) -> None:
    exposed = "sk-ant-api03-should-never-reach-a-trace"

    class _Boom(LLMAdapter):
        provider_name = "boom"

        async def generate(self, request):  # type: ignore[override]
            raise RuntimeError(f"Authorization: Bearer {exposed}")

    with caplog.at_level("WARNING"):
        invocation = await _invoker(_Boom()).invoke(
            _record(),
            message="go",
            triggered_by="sam@example.com",
        )

    assert exposed not in invocation.error
    assert exposed not in caplog.text
    assert "Bearer ***" in invocation.error


async def test_prompt_and_model_output_secrets_are_redacted_from_the_stored_trace() -> None:
    exposed = "sk-ant-api03-should-never-be-persisted"
    store = TraceStore(None)

    invocation = await _invoker(
        _ScriptedLLM([LLMResponse(text=f"The supplied token was {exposed}")]),
        store=store,
    ).invoke(
        _record(),
        message=f"Use {exposed}",
        triggered_by="sam@example.com",
    )
    stored = await store.get("sb-1", invocation.id)

    assert stored is not None
    assert exposed not in __import__("json").dumps(stored.to_dict())
    assert "***" in stored.message
    assert "***" in stored.final_text


async def test_a_sandbox_never_resolves_or_falls_back_to_principal_credentials() -> None:
    user_llm = _ScriptedLLM([LLMResponse(text="leaked user key")])
    platform_llm = _ScriptedLLM([LLMResponse(text="leaked platform key")])

    class _UserLLMResolver:
        calls = 0

        async def resolve(self, principal):
            self.calls += 1
            return user_llm

    class _UserSCMResolver:
        calls = 0

        async def resolve(self, principal):
            self.calls += 1
            return object()

    llm_resolver = _UserLLMResolver()
    scm_resolver = _UserSCMResolver()
    invoker = SandboxInvoker(
        specializations=_Specs(_registry()),
        deps=StageDeps(
            config=Settings(),
            llm=platform_llm,
            scm=object(),  # type: ignore[arg-type]
            llm_resolver=llm_resolver,
            scm_resolver=scm_resolver,
        ),
        traces=TraceStore(None),
    )

    invocation = await invoker.invoke(
        _record(),
        message="try every credential",
        triggered_by="sam@example.com",
    )

    assert not invocation.ok
    assert "sandbox LLM connector" in invocation.error
    assert llm_resolver.calls == 0
    assert scm_resolver.calls == 0
    assert user_llm.calls == 0
    assert platform_llm.calls == 0


_DRAFT = {
    "apiVersion": "registry.agentic.dev/v1alpha1",
    "kind": "Agent",
    "metadata": {"name": "draft-notes-writer"},
    "spec": {
        "title": "Draft Notes Writer",
        "systemPrompt": "You write release notes, badly, for now.",
        "builtinTools": ["scm_list_files"],
    },
}


async def test_a_draft_agent_runs_without_being_published() -> None:
    # Trying the definition before it exists in the catalog is the point of the
    # studio: publishing something untested is what it removes.
    llm = _ScriptedLLM([LLMResponse(text="draft notes")])
    inv = _invoker(llm, registry=_Specs(SpecializationRegistry()))

    result = await inv.invoke(
        _record(agent="draft-notes-writer", draft=_DRAFT), message="go", triggered_by="sam@example.com"
    )

    assert result.ok
    assert result.agent == "draft_notes_writer"
    assert result.steps[0].output == "You write release notes, badly, for now."


async def test_a_draft_beats_a_published_agent_of_the_same_name() -> None:
    llm = _ScriptedLLM([LLMResponse(text="draft notes")])
    draft = {**_DRAFT, "metadata": {"name": "release-notes-writer"}}

    result = await _invoker(llm).invoke(_record(draft=draft), message="go", triggered_by="sam@example.com")

    assert result.steps[0].output == "You write release notes, badly, for now."


async def test_an_agent_published_after_startup_is_invokable(monkeypatch) -> None:
    # The invoker asks the catalog, not a snapshot of it: publishing an agent
    # and immediately testing it in a sandbox is the loop, not a restart away.
    registry = SpecializationRegistry()
    specs = _Specs(registry)
    inv = _invoker(_ScriptedLLM([LLMResponse(text="notes")]), registry=specs)

    with pytest.raises(ValueError, match="not runnable"):
        await inv.invoke(_record(), message="go", triggered_by="sam@example.com")

    registry.register(load_specialization_from_string(_SPEC_YAML))

    result = await inv.invoke(_record(), message="go", triggered_by="sam@example.com")
    assert result.ok


class _Catalog:
    """Stands in for the registry client the invoker falls back to."""

    def __init__(self, raw) -> None:
        self._raw = raw

    def get_agent(self, name):
        from types import SimpleNamespace

        return SimpleNamespace(raw=self._raw) if self._raw else None


async def test_a_published_catalog_agent_runs_from_its_registry_envelope() -> None:
    # User-published records never join the reviewed role catalog; the sandbox
    # fence is the boundary, so the registry envelope is enough to run one.
    raw = {
        "metadata": {"name": "measure-mate-agent", "tag": "1"},
        "spec": {"systemPrompt": "Convert units precisely.", "description": "Converts units."},
    }
    llm = _ScriptedLLM([LLMResponse(text="30.5 cm")])
    inv = _invoker(llm, registry=_Specs(SpecializationRegistry()), catalog=_Catalog(raw))

    result = await inv.invoke(_record(agent="measure-mate-agent"), message="1 ft in cm", triggered_by="sam@example.com")

    assert result.ok
    assert result.final_text == "30.5 cm"
    assert result.steps[0].output == "Convert units precisely."


async def test_a_reviewed_agent_failing_governed_admission_falls_back_to_its_envelope() -> None:
    # Governed admission (version/label/policy match) guards live runs; inside
    # the sandbox fence an inadmissible reviewed bundle must degrade to the
    # registry envelope instead of surfacing a 500.
    from devai.specializations.service import AgentUnavailableError

    class _InadmissibleSpecs:
        async def resolve_runnable(self, name: str):
            raise AgentUnavailableError("agent version is not admitted")

    raw = {
        "metadata": {"name": "capacity-planner-agent", "tag": "1.0.1"},
        "spec": {"systemPrompt": "Plan capacity.", "description": "Plans capacity."},
    }
    llm = _ScriptedLLM([LLMResponse(text="plan for 9 replicas")])
    inv = _invoker(llm, registry=_InadmissibleSpecs(), catalog=_Catalog(raw))

    result = await inv.invoke(
        _record(agent="capacity-planner-agent"), message="traffic doubles", triggered_by="sam@example.com"
    )

    assert result.ok
    assert result.final_text == "plan for 9 replicas"


async def test_an_agent_unknown_to_roles_and_registry_stays_not_runnable() -> None:
    inv = _invoker(
        _ScriptedLLM([LLMResponse(text="never")]),
        registry=_Specs(SpecializationRegistry()),
        catalog=_Catalog(None),
    )

    with pytest.raises(ValueError, match="not runnable"):
        await inv.invoke(_record(agent="ghost-agent"), message="hi", triggered_by="sam@example.com")
