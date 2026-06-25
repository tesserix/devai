"""Autonomy + gate + resilience tests.

The contract the user experience depends on:
  - autonomy=full/auto → static gates self-approve; runs flow end-to-end.
  - autonomy=gated → static gates pause for the human decision key.
  - plan_approval (smart gate) pauses ONLY for ambiguous intents, with a
    rich approval request (questions/plan/tech) registered as a dynamic gate.
  - transient stage failures retry with backoff before on_failure applies.
"""

from __future__ import annotations

import asyncio

import pytest

from devai.blueprint.executor import BlueprintExecutor
from devai.blueprint.loader import Blueprint, StageSpec
from devai.blueprint.registry import StageRegistry
from devai.pipeline.interfaces import PipelineStage, StageDeps
from devai.pipeline.types import DevAITask, StageResult, TaskState


class _FakeRedis:
    def __init__(self):
        self.data: dict[str, str] = {}

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, ex=None):
        self.data[key] = value


class _SM:
    def __init__(self):
        self.redis = _FakeRedis()

    async def get_pipeline_control(self, task_id):
        return None


class _Cfg:
    pipeline_label = "x"
    pipeline_default_autonomy = "auto"
    pipeline_stage_retries = 1


class _OkStage(PipelineStage):
    def __init__(self, *a, **kw):
        pass

    def name(self):
        return "ok"

    async def execute(self, task):
        return StageResult(message="done")


def _executor(sm=None, registry=None):
    reg = registry or StageRegistry()
    if not reg.has("noop_ok"):
        reg.register("noop_ok", lambda deps, cfg: _OkStage())
    deps = StageDeps(config=_Cfg(), state_manager=sm or _SM())
    return BlueprintExecutor(reg, deps), reg


def _bp(stages):
    return Blueprint(name="t", description="", stages=stages)


# ──────────────────────────────────────────────────────────────────────
# Static gates × autonomy
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_static_gate_self_approves_only_under_full_autonomy():
    sm = _SM()
    ex, _ = _executor(sm)
    task = DevAITask(intent="ship", blueprint="t", repo="o/r")
    task.agent_context["autonomy"] = "full"
    await ex.execute(_bp([StageSpec(name="deploy", stage="noop_ok", gate=True)]), task)

    assert task.state == TaskState.COMPLETED
    assert "deploy" in task.stages_completed
    assert sm.redis.data[f"devai:pipeline:gate:{task.id}:deploy"] == "approved"
    assert sm.redis.data[f"devai:pipeline:gate:{task.id}:deploy:approver"] == "autonomy:full"
    assert any("auto-approved" in (e.message or "") for e in task.stage_events)


@pytest.mark.asyncio
async def test_static_gate_under_auto_waits_and_times_out_resumably():
    """Smart autonomy NO LONGER self-approves hard gates: it pauses for the
    human and a timeout lands in stage_failed (Continue re-requests) —
    never a silent approval, never an unresumable cancel."""
    sm = _SM()
    ex, _ = _executor(sm)
    ex._GATE_POLL_SECONDS = 0.01
    ex._deps.config.pipeline_gate_timeout_seconds = 0.05
    task = DevAITask(intent="ship", blueprint="t", repo="o/r")  # autonomy default = auto
    await ex.execute(_bp([StageSpec(name="deploy", stage="noop_ok", gate=True)]), task)

    assert task.state == TaskState.STAGE_FAILED
    assert "deploy" in task.stages_failed
    assert "press Continue" in (task.error or "")
    assert "deploy" not in task.stages_completed  # resume re-reaches the gate


@pytest.mark.asyncio
async def test_static_gate_blocks_until_decision_when_gated():
    sm = _SM()
    ex, _ = _executor(sm)
    ex._GATE_POLL_SECONDS = 0.01
    task = DevAITask(intent="ship", blueprint="t", repo="o/r")
    task.agent_context["autonomy"] = "gated"
    key = f"devai:pipeline:gate:{task.id}:deploy"

    async def approve_later():
        await asyncio.sleep(0.05)
        await sm.redis.set(key, "approved")

    approver = asyncio.create_task(approve_later())
    await ex.execute(_bp([StageSpec(name="deploy", stage="noop_ok", gate=True)]), task)
    await approver

    assert task.state == TaskState.COMPLETED
    assert "deploy" in task.stages_completed
    assert any("waiting for your approval" in (e.message or "") for e in task.stage_events)


@pytest.mark.asyncio
async def test_static_gate_rejection_cancels_run():
    sm = _SM()
    ex, _ = _executor(sm)
    task = DevAITask(intent="ship", blueprint="t", repo="o/r")
    task.agent_context["autonomy"] = "gated"
    await sm.redis.set(f"devai:pipeline:gate:{task.id}:deploy", "rejected")

    await ex.execute(_bp([StageSpec(name="deploy", stage="noop_ok", gate=True)]), task)
    assert task.state == TaskState.CANCELLED
    assert "rejected at gate" in (task.error or "")
    assert "deploy" not in task.stages_completed


# ──────────────────────────────────────────────────────────────────────
# Mid-stage STOP — the run halts NOW, not at the next level boundary
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stop_interrupts_a_running_stage():
    cancelled = {"flag": False}

    class _SlowStage(PipelineStage):
        def name(self):
            return "slow"

        async def execute(self, task):
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled["flag"] = True
                raise
            return StageResult(message="never")

    class _StoppableSM(_SM):
        def __init__(self):
            super().__init__()
            self.control = "running"

        async def get_pipeline_control(self, task_id):
            return self.control

    sm = _StoppableSM()
    reg = StageRegistry()
    reg.register("slow", lambda deps, cfg: _SlowStage())
    ex, _ = _executor(sm=sm, registry=reg)
    ex._CONTROL_POLL_SECONDS = 0.02

    task = DevAITask(intent="x", blueprint="t", repo="o/r")

    async def stop_soon():
        await asyncio.sleep(0.1)
        sm.control = "stopped"

    stopper = asyncio.create_task(stop_soon())
    await asyncio.wait_for(ex.execute(_bp([StageSpec(name="s1", stage="slow", timeout_seconds=60)]), task), timeout=5)
    await stopper

    assert task.state == TaskState.CANCELLED
    assert task.error == "stopped by user"
    assert cancelled["flag"] is True  # the in-flight stage coroutine was cancelled
    assert "s1" not in task.stages_completed
    assert any("stopped by user mid-stage" in (e.error or "") for e in task.stage_events)


@pytest.mark.asyncio
async def test_stop_does_not_trigger_retry():
    """A user stop must never be retried as if it were a transient failure."""
    attempts = {"n": 0}

    class _SlowStage(PipelineStage):
        def name(self):
            return "slow"

        async def execute(self, task):
            attempts["n"] += 1
            await asyncio.sleep(30)
            return StageResult()

    class _StoppedSM(_SM):
        async def get_pipeline_control(self, task_id):
            return "stopped"

    reg = StageRegistry()
    reg.register("slow", lambda deps, cfg: _SlowStage())
    ex, _ = _executor(sm=_StoppedSM(), registry=reg)
    ex._CONTROL_POLL_SECONDS = 0.02
    task = DevAITask(intent="x", blueprint="t", repo="o/r")
    # execute() checks control at the level boundary first and cancels there;
    # exercise _run_one directly to prove the mid-stage path also stops once.
    spec = StageSpec(name="s1", stage="slow", timeout_seconds=60)
    await asyncio.wait_for(ex._run_one(spec, task), timeout=5)
    assert attempts["n"] == 1
    assert task.state == TaskState.CANCELLED


# ──────────────────────────────────────────────────────────────────────
# Transient-failure retries
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stage_retries_transient_failure_then_succeeds():
    attempts = {"n": 0}

    class _Flaky(PipelineStage):
        def name(self):
            return "flaky"

        async def execute(self, task):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("LLM hiccup")
            return StageResult(message="recovered")

    reg = StageRegistry()
    reg.register("flaky", lambda deps, cfg: _Flaky())
    ex, _ = _executor(registry=reg)
    task = DevAITask(intent="x", blueprint="t", repo="o/r")
    await ex.execute(_bp([StageSpec(name="s1", stage="flaky")]), task)

    assert attempts["n"] == 2
    assert task.state == TaskState.COMPLETED
    assert "s1" in task.stages_completed
    assert any("retrying after failure" in (e.message or "") for e in task.stage_events)


@pytest.mark.asyncio
async def test_stage_fails_after_retries_exhausted():
    class _AlwaysBoom(PipelineStage):
        def name(self):
            return "boom"

        async def execute(self, task):
            raise RuntimeError("permanent")

    reg = StageRegistry()
    reg.register("boom", lambda deps, cfg: _AlwaysBoom())
    ex, _ = _executor(registry=reg)
    task = DevAITask(intent="x", blueprint="t", repo="o/r")
    await ex.execute(_bp([StageSpec(name="s1", stage="boom")]), task)

    assert task.is_failed
    assert "s1" in task.stages_failed


# ──────────────────────────────────────────────────────────────────────
# Smart plan approval
# ──────────────────────────────────────────────────────────────────────


class _FakeLLM:
    provider_name = "anthropic"

    def __init__(self, text):
        self._text = text

    async def generate(self, request):
        text = self._text

        class _R:
            pass

        r = _R()
        r.text = text
        return r


def _plan_stage(sm=None, llm=None, autonomy=None):
    from devai.pipeline.stages.lifecycle import plan_approval_stage

    deps = StageDeps(config=_Cfg(), state_manager=sm, llm=llm)
    stage = plan_approval_stage(deps, {"__stage_name": "plan-approval"})
    task = DevAITask(intent="build me the petstore with cart and login", blueprint="alm-pipeline", repo="o/r")
    if autonomy:
        task.agent_context["autonomy"] = autonomy
    return stage, task


@pytest.mark.asyncio
async def test_plan_approval_skips_when_full():
    stage, task = _plan_stage(autonomy="full")
    result = await stage.execute(task)
    assert result.data["plan_approved"] == "auto"


@pytest.mark.asyncio
async def test_plan_approval_clear_intent_proceeds_without_pause():
    stage, task = _plan_stage(sm=_SM(), llm=_FakeLLM("CLEAR"))
    result = await stage.execute(task)
    assert result.data["plan_approved"] == "auto-clear"
    assert task.agent_context.get("dynamic_gates") is None


@pytest.mark.asyncio
async def test_plan_approval_ambiguous_pauses_with_rich_request_then_approves():
    sm = _SM()
    stage, task = _plan_stage(
        sm=sm,
        llm=_FakeLLM("AMBIGUOUS\nWhich database should the petstore use?\nIs authentication social or email/password?"),
    )
    task.agent_context["detected_tech_stack"] = "nextjs+fastapi"
    task.agent_context["engineering_manager_output"] = {"technical_plan": "Build API first, then UI."}
    task.epic_issue_number = 42
    # Pre-approve so the wait loop returns immediately.
    await sm.redis.set(f"devai:pipeline:gate:{task.id}:plan-approval", "approved")

    result = await stage.execute(task)

    assert result.data["plan_approved"] == "human"
    gates = task.agent_context["dynamic_gates"]
    assert len(gates) == 1
    g = gates[0]
    assert g["kind"] == "plan_approval"
    assert g["tech_stack"] == "nextjs+fastapi"
    assert g["plan_summary"] == "Build API first, then UI."
    assert g["epic_issue_number"] == 42
    assert len(g["questions"]) == 2
    assert "database" in g["questions"][0]


@pytest.mark.asyncio
async def test_plan_approval_rejection_cancels():
    sm = _SM()
    stage, task = _plan_stage(sm=sm, llm=_FakeLLM("AMBIGUOUS\nWhat tech stack?"))
    await sm.redis.set(f"devai:pipeline:gate:{task.id}:plan-approval", "rejected")
    result = await stage.execute(task)
    assert result.next_state == TaskState.CANCELLED
    assert "rejected" in (task.error or "")


@pytest.mark.asyncio
async def test_plan_approval_no_llm_never_blocks():
    stage, task = _plan_stage(sm=_SM(), llm=None)
    result = await stage.execute(task)
    assert result.data["plan_approved"] == "auto-clear"


# ──────────────────────────────────────────────────────────────────────
# Self-healing test loop: diagnose → bug issue → fix brief → re-test
# ──────────────────────────────────────────────────────────────────────


class _IssueSCM:
    def __init__(self):
        self.issues: list[dict] = []

    async def create_issue(self, repo, title, body, labels=None):
        n = 100 + len(self.issues)
        self.issues.append({"repo": repo, "title": title, "body": body, "labels": labels})
        return {"number": n, "html_url": f"https://github.com/{repo}/issues/{n}"}


@pytest.mark.asyncio
async def test_diagnose_skips_when_no_failures():
    from devai.pipeline.stages.alm import diagnose_test_failures_stage

    stage = diagnose_test_failures_stage(StageDeps(config=_Cfg()), {})
    task = DevAITask(intent="x", blueprint="alm-pipeline", repo="o/r")
    result = await stage.execute(task)
    assert result.data.get("diagnosis_skipped") is True


@pytest.mark.asyncio
async def test_diagnose_files_linked_bug_with_root_cause():
    from devai.pipeline.stages.alm import diagnose_test_failures_stage

    scm = _IssueSCM()
    llm = _FakeLLM("ROOT CAUSE: The cart total ignores quantity.\nFIX: Multiply unit price by quantity in cart.ts.")
    stage = diagnose_test_failures_stage(StageDeps(config=_Cfg(), scm=scm, llm=llm), {})
    task = DevAITask(intent="petstore", blueprint="alm-pipeline", repo="o/r")
    task.agent_context["test_failed"] = 2
    task.agent_context["qa_tester_output"] = {"failed": 2, "summary": "cart_total spec failing"}
    task.epic_issue_number = 42
    task.story_issue_numbers = [43, 44]
    task.pr_number = 7

    result = await stage.execute(task)

    assert result.data["bug_issue_number"] == 100
    assert "cart total ignores quantity" in result.data["test_fix_brief"]
    issue = scm.issues[0]
    assert "devai:bug" in issue["labels"]
    assert "#42" in issue["body"]  # epic link
    assert "#43" in issue["body"] and "#44" in issue["body"]  # story links
    assert "#7" in issue["body"]  # PR link
    assert "Root cause analysis" in issue["body"]


@pytest.mark.asyncio
async def test_diagnose_dry_run_files_no_issue_but_briefs_the_fix():
    from devai.pipeline.stages.alm import diagnose_test_failures_stage

    scm = _IssueSCM()
    stage = diagnose_test_failures_stage(StageDeps(config=_Cfg(), scm=scm, llm=None), {})
    task = DevAITask(intent="x", blueprint="alm-pipeline", repo="o/r")
    task.dry_run = True
    task.agent_context["test_failed"] = 1
    result = await stage.execute(task)
    assert scm.issues == []
    assert result.data["bug_issue_number"] is None
    assert result.data["test_fix_brief"]


@pytest.mark.asyncio
async def test_product_director_dispatches_epic_vs_stories_by_stage():
    """The blueprint adapter calls generic run() for BOTH planning stages —
    the old default always ran run_stories, so blueprint runs never created
    an epic (no epic issue → no story links, no supervision thread)."""
    pytest.importorskip("ulid")  # slim envs lack the legacy agent deps
    from devai.agents.product_director import ProductDirectorAgent

    agent = ProductDirectorAgent.__new__(ProductDirectorAgent)
    calls: list[str] = []

    async def fake_epic(state, a2a=None):
        calls.append("epic")
        return {}

    async def fake_stories(state, a2a=None):
        calls.append("stories")
        return {}

    agent.run_epic = fake_epic  # type: ignore[method-assign]
    agent.run_stories = fake_stories  # type: ignore[method-assign]

    await agent._execute_graph({"stage": "create-epic"}, a2a=None)
    await agent._execute_graph({"stage": "create_stories"}, a2a=None)
    await agent._execute_graph({"stage": ""}, a2a=None)  # legacy default
    assert calls == ["epic", "stories", "stories"]


def test_create_stories_stage_constructs_real_agent():
    """create_stories was a stub (_make_agent -> None) — it must run the
    ProductDirector so run_stories executes with the epic handover. Now an
    AgentStage whose legacy bridge targets the real ProductDirectorAgent."""
    from devai.pipeline.stages.agent_stage import AgentStage
    from devai.pipeline.stages.alm import create_stories_stage

    stage = create_stories_stage(StageDeps(config=_Cfg()), {})
    assert isinstance(stage, AgentStage)
    assert stage.output_key == "story_creator"
    assert "ProductDirectorAgent" in stage._agent._dotted


def test_fix_stage_briefs_developer_and_marks_fix_applied():
    from devai.pipeline.stages.alm import _fix_test_failures_instruction, fix_test_failures_stage

    task = DevAITask(intent="petstore", blueprint="alm-pipeline", repo="o/r")
    task.agent_context["bug_issue_number"] = 100
    task.agent_context["test_fix_brief"] = "Multiply unit price by quantity."

    # The fix brief overrides task.intent (build_alm_state puts instruction →
    # requirements) and the stage marks the handover as a fix.
    brief = _fix_test_failures_instruction(task)
    assert "bug #100" in brief
    assert "Multiply unit price by quantity." in brief
    assert "SMALLEST fix" in brief

    stage = fix_test_failures_stage(StageDeps(config=_Cfg()), {})
    assert stage._extra_data == {"test_fix_applied": True}


@pytest.mark.asyncio
async def test_implement_stage_requires_pull_request():
    """Implementation that produces no PR (no commits) must FAIL, not flow
    downstream as narrative-only success (the 51-minute empty run)."""
    from devai.pipeline.stages.alm import _validate_pull_request

    deps = StageDeps(config=_Cfg())
    task = DevAITask(intent="x", blueprint="alm-pipeline", repo="o/r")
    with pytest.raises(RuntimeError, match="no pull request"):
        await _validate_pull_request(deps, task, {"implementation_summary": "I looked around but committed nothing"})

    # With a PR the contract passes (no SCM wired → labeling skipped).
    await _validate_pull_request(deps, task, {"pr_number": 12})


def test_run_correlation_label_format():
    from devai.pipeline.stages._base import run_correlation_label

    assert run_correlation_label("devai-c0ccd293f7b4") == "devai:run:c0ccd293f7"


@pytest.mark.asyncio
async def test_quality_gate_stages_reject_empty_outputs():
    """0.0s 'completed' reviews/scans/tests with no verdict are silent no-ops
    — every quality-gate agent must produce its decision fields."""
    from devai.pipeline.stages.alm import _require_outputs, _validate_run_tests

    deps = StageDeps(config=_Cfg())
    task = DevAITask(intent="x", blueprint="alm-pipeline", repo="o/r")
    review = _require_outputs(("review_decision",))
    security = _require_outputs(("security_decision",))

    with pytest.raises(RuntimeError, match="review_decision"):
        await review(deps, task, {"summary": "looks fine"}, stage_name="review_code", output_key="staff_reviewer")
    await review(deps, task, {"review_decision": "approved"}, stage_name="review_code", output_key="staff_reviewer")

    with pytest.raises(RuntimeError, match="security_decision"):
        await security(deps, task, {}, stage_name="security_scan", output_key="security_expert")
    await security(deps, task, {"security_decision": "pass"}, stage_name="security_scan", output_key="security_expert")

    with pytest.raises(RuntimeError, match="no test results"):
        await _validate_run_tests(deps, task, {"summary": "ran around"}, stage_name="run_tests", output_key="qa_tester")
    await _validate_run_tests(
        deps,
        task,
        {"test_total": 5, "test_passed": 5, "test_failed": 0},
        stage_name="run_tests",
        output_key="qa_tester",
    )
    # Stub path stays silent-tolerant (slim envs): the {output_key}_stub key short-circuits.
    await review(deps, task, {"staff_reviewer_stub": True}, stage_name="review_code", output_key="staff_reviewer")
