"""Autonomous failure recovery (the executor's recovery agent).

The contract: when a stage exhausts its transient retries, a recovery agent
reviews the failure BEFORE on_failure semantics apply —

  - action=retry    → corrective guidance lands in agent_context and the
                      stage re-runs; success continues the pipeline.
  - action=ask_user → a dynamic heal gate pauses the run for the human
                      (self-approved under autonomy=full).
  - action=abort / no LLM / unusable response → the failure stands and the
                      blueprint's on_failure semantics apply unchanged.
"""

from __future__ import annotations

import json

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
    pipeline_stage_retries = 0  # isolate recovery from transient retries
    pipeline_heal_on_failure = True
    pipeline_heal_attempts = 1
    pipeline_stage_inactivity_grace = 240
    pipeline_stage_hard_cap_multiplier = 100  # tests use sub-second timeouts


class _FakeLLM:
    """Scripted recovery-specialist responses."""

    provider_name = "fake"

    def __init__(self, decision: dict):
        self.decision = decision
        self.calls: list[str] = []

    async def generate(self, request):
        self.calls.append(request.messages[0].content)

        class _R:
            text = json.dumps(self.decision)

        return _R()


class _FlakyStage(PipelineStage):
    """Fails until it sees recovery guidance on the task."""

    def __init__(self, task_ctx_key: str):
        self.key = task_ctx_key
        self.attempts = 0

    def name(self):
        return "flaky"

    async def execute(self, task):
        self.attempts += 1
        if not task.agent_context.get(self.key):
            raise RuntimeError("stories response unparseable")
        return StageResult(message="fixed on retry")


class _AlwaysFails(PipelineStage):
    def name(self):
        return "broken"

    async def execute(self, task):
        raise RuntimeError("permanently broken")


def _executor(llm=None, sm=None, stage_factory=None, autonomy="auto"):
    reg = StageRegistry()
    reg.register("work", stage_factory or (lambda deps, cfg: _AlwaysFails()))
    cfg = _Cfg()
    cfg.pipeline_default_autonomy = autonomy
    deps = StageDeps(config=cfg, state_manager=sm or _SM(), llm=llm)
    return BlueprintExecutor(reg, deps)


def _bp(**spec_kw):
    return Blueprint(
        name="t", description="", stages=[StageSpec(name="build", stage="work", **spec_kw)]
    )


@pytest.mark.asyncio
async def test_retry_decision_reruns_stage_with_guidance():
    llm = _FakeLLM(
        {"diagnosis": "LLM wrapped JSON in fences", "action": "retry", "guidance": "strip markdown fences"}
    )
    stage = _FlakyStage("heal:build")
    ex = _executor(llm=llm, stage_factory=lambda deps, cfg: stage)
    task = DevAITask(intent="ship", blueprint="t", repo="o/r")
    await ex.execute(_bp(), task)

    assert task.state == TaskState.COMPLETED
    assert "build" in task.stages_completed
    assert stage.attempts == 2  # failed once, healed, succeeded
    heal = task.agent_context["heal:build"]
    assert heal["guidance"] == "strip markdown fences"
    assert any(e.stage == "heal:build" and e.agent == "recovery_specialist" for e in task.stage_events)
    # the failure prompt reached the recovery agent
    assert "stories response unparseable" in llm.calls[0]


@pytest.mark.asyncio
async def test_abort_decision_lets_failure_stand():
    llm = _FakeLLM({"diagnosis": "credentials revoked", "action": "abort", "guidance": ""})
    ex = _executor(llm=llm)
    task = DevAITask(intent="ship", blueprint="t", repo="o/r")
    await ex.execute(_bp(), task)

    assert task.state == TaskState.STAGE_FAILED
    assert "build" in task.stages_failed
    assert any(e.stage == "heal:build" and "abandoned" in (e.error or "") for e in task.stage_events)


@pytest.mark.asyncio
async def test_no_llm_falls_back_to_on_failure_semantics():
    ex = _executor(llm=None)
    task = DevAITask(intent="ship", blueprint="t", repo="o/r")
    await ex.execute(_bp(on_failure="continue"), task)

    # heal degraded; on_failure=continue keeps the old behavior
    assert task.state == TaskState.COMPLETED
    assert "build" in task.stages_failed


@pytest.mark.asyncio
async def test_recovery_rounds_are_bounded():
    llm = _FakeLLM({"diagnosis": "try again", "action": "retry", "guidance": "same thing"})
    ex = _executor(llm=llm)  # stage always fails, heal_attempts=1
    task = DevAITask(intent="ship", blueprint="t", repo="o/r")
    await ex.execute(_bp(), task)

    assert task.state == TaskState.STAGE_FAILED
    # Exactly one DIAGNOSIS round (no infinite loop); the runbook-advice call
    # after exhaustion shares the same fake LLM, so filter by prompt.
    diagnosis_calls = [c for c in llm.calls if "Decide how to recover" in c]
    assert len(diagnosis_calls) == 1
    # Exhaustion leaves the runbook on the task for the dashboard/human.
    assert "runbook:build" in task.agent_context


@pytest.mark.asyncio
async def test_ask_user_self_approves_under_full_autonomy():
    llm = _FakeLLM(
        {
            "diagnosis": "needs decision",
            "action": "ask_user",
            "guidance": "use mock payments",
            "user_message": "OK to mock payments?",
        }
    )
    stage = _FlakyStage("heal:build")
    ex = _executor(llm=llm, stage_factory=lambda deps, cfg: stage, autonomy="full")
    task = DevAITask(intent="ship", blueprint="t", repo="o/r")
    await ex.execute(_bp(), task)

    assert task.state == TaskState.COMPLETED  # no pause under full autonomy
    assert not task.agent_context.get("dynamic_gates")


@pytest.mark.asyncio
async def test_ask_user_raises_dynamic_gate_and_approval_reruns():
    llm = _FakeLLM(
        {
            "diagnosis": "schema choice unclear",
            "action": "ask_user",
            "guidance": "use a flat schema",
            "user_message": "Flatten the schema?",
        }
    )
    sm = _SM()
    stage = _FlakyStage("heal:build")
    ex = _executor(llm=llm, sm=sm, stage_factory=lambda deps, cfg: stage)
    task = DevAITask(intent="ship", blueprint="t", repo="o/r")
    # pre-write the human's approval so the poll resolves immediately
    sm.redis.data[f"devai:pipeline:gate:{task.id}:heal-build"] = "approved"
    await ex.execute(_bp(), task)

    assert task.state == TaskState.COMPLETED
    gates = task.agent_context["dynamic_gates"]
    assert gates and gates[0]["gate"] == "heal-build"
    assert gates[0]["kind"] == "heal_approval"
    assert gates[0]["questions"] == ["Flatten the schema?"]


@pytest.mark.asyncio
async def test_ask_user_rejection_lets_failure_stand():
    llm = _FakeLLM(
        {"diagnosis": "d", "action": "ask_user", "guidance": "g", "user_message": "ok?"}
    )
    sm = _SM()
    ex = _executor(llm=llm, sm=sm)
    task = DevAITask(intent="ship", blueprint="t", repo="o/r")
    sm.redis.data[f"devai:pipeline:gate:{task.id}:heal-build"] = "rejected"
    await ex.execute(_bp(), task)

    assert task.state == TaskState.STAGE_FAILED
    assert "build" in task.stages_failed


@pytest.mark.asyncio
async def test_timeout_with_flaky_diagnosis_still_retries():
    """A TIMEOUT must never be abandoned because the diagnosis LLM flaked —
    the fallback decision retries with incremental-continuation guidance."""

    class _GarbageLLM:
        provider_name = "fake"

        async def generate(self, request):
            class _R:
                text = "sorry, I cannot help with that"  # unparseable → decision None

            return _R()

    class _SlowThenHealedStage(PipelineStage):
        def __init__(self):
            self.attempts = 0

        def name(self):
            return "slow"

        async def execute(self, task):
            self.attempts += 1
            if not task.agent_context.get("heal:build"):
                import asyncio

                await asyncio.sleep(5)  # exceeds the 0.05s stage timeout
            return StageResult(message="finished within budget on retry")

    stage = _SlowThenHealedStage()
    ex = _executor(llm=_GarbageLLM(), stage_factory=lambda deps, cfg: stage)
    task = DevAITask(intent="ship", blueprint="t", repo="o/r")
    bp = Blueprint(
        name="t",
        description="",
        stages=[StageSpec(name="build", stage="work", timeout_seconds=0.05)],
    )
    await ex.execute(bp, task)

    assert task.state == TaskState.COMPLETED
    heal = task.agent_context["heal:build"]
    assert "remaining work" in heal["guidance"]
    assert stage.attempts == 2


@pytest.mark.asyncio
async def test_active_stage_outlives_its_timeout():
    """Progress-aware liveness: a stage past its deadline with a FRESH
    tool-activity heartbeat is extended instead of killed."""
    import asyncio
    import time as _time

    class _Working(PipelineStage):
        def name(self):
            return "busy"

        async def execute(self, task):
            await asyncio.sleep(3.0)  # far beyond the 0.1s timeout
            return StageResult(message="finished while heartbeating")

    sm = _SM()
    ex = _executor(sm=sm, stage_factory=lambda deps, cfg: _Working())
    # Speed the supervision poll up for the test.
    ex._CONTROL_POLL_SECONDS = 0.2
    task = DevAITask(intent="ship", blueprint="t", repo="o/r")
    sm.redis.data[f"devai:run:{task.id}:activity"] = str(_time.time())
    bp = Blueprint(
        name="t", description="", stages=[StageSpec(name="build", stage="work", timeout_seconds=0.1)]
    )
    await ex.execute(bp, task)

    assert task.state == TaskState.COMPLETED
    assert "build" in task.stages_completed


@pytest.mark.asyncio
async def test_stalled_stage_still_dies_at_timeout():
    """No heartbeat → the original timeout semantics stand."""
    import asyncio

    class _Stalled(PipelineStage):
        def name(self):
            return "stuck"

        async def execute(self, task):
            await asyncio.sleep(3.0)
            return StageResult(message="never reached")

    sm = _SM()  # no activity key
    ex = _executor(llm=None, sm=sm, stage_factory=lambda deps, cfg: _Stalled())
    ex._CONTROL_POLL_SECONDS = 0.2
    task = DevAITask(intent="ship", blueprint="t", repo="o/r")
    bp = Blueprint(
        name="t", description="", stages=[StageSpec(name="build", stage="work", timeout_seconds=0.1)]
    )
    await ex.execute(bp, task)

    assert task.state == TaskState.AGENT_TIMEOUT
    assert "build" in task.stages_failed


class _FakeSCM:
    def __init__(self):
        self.issues: list[dict] = []
        self.comments: list[tuple[int, str]] = []
        self.labels: list[tuple[int, list[str]]] = []

    async def create_issue(self, repo, title, body, labels=None):
        self.issues.append({"title": title, "body": body, "labels": labels or []})
        return {"number": 99, "html_url": "https://github.com/o/r/issues/99"}

    async def add_comment(self, repo, issue_id, body):
        self.comments.append((issue_id, body))
        return {}

    async def add_labels(self, repo, issue_id, labels):
        self.labels.append((issue_id, labels))


@pytest.mark.asyncio
async def test_bug_issue_filed_updated_and_runbook_on_exhaustion():
    """The recovery agent's durable trail: round 1 files a labeled bug, each
    further round comments the new diagnosis, exhaustion posts the runbook
    and flags the bug for a human."""
    llm = _FakeLLM({"diagnosis": "still broken", "action": "retry", "guidance": "try again"})
    scm = _FakeSCM()
    reg = StageRegistry()
    reg.register("work", lambda deps, cfg: _AlwaysFails())
    cfg = _Cfg()
    cfg.pipeline_heal_attempts = 2
    deps = StageDeps(config=cfg, state_manager=_SM(), llm=llm, scm=scm)
    ex = BlueprintExecutor(reg, deps)
    task = DevAITask(intent="ship", blueprint="t", repo="o/r")
    task.epic_issue_number = 26
    await ex.execute(_bp(), task)

    assert task.state == TaskState.STAGE_FAILED
    # Round 1 filed the bug with run-correlation + failure labels.
    assert len(scm.issues) == 1
    assert "devai:stage-failure" in scm.issues[0]["labels"]
    assert any(lbl.startswith("devai:run:") for lbl in scm.issues[0]["labels"])
    assert "Epic:** #26" in scm.issues[0]["body"]
    # Round 2 commented; exhaustion posted the runbook comment.
    bodies = [b for _, b in scm.comments]
    assert any("Recovery attempt 2" in b for b in bodies)
    assert any("Runbook" in b for b in bodies)
    assert ("devai:needs-human" in lbls for _, lbls in scm.labels)
    assert task.agent_context["heal:build"]["bug_issue"] == 99


@pytest.mark.asyncio
async def test_heal_disabled_via_config():
    llm = _FakeLLM({"diagnosis": "x", "action": "retry", "guidance": "y"})
    reg = StageRegistry()
    reg.register("work", lambda deps, cfg: _AlwaysFails())
    cfg = _Cfg()
    cfg.pipeline_heal_on_failure = False
    deps = StageDeps(config=cfg, state_manager=_SM(), llm=llm)
    ex = BlueprintExecutor(reg, deps)
    task = DevAITask(intent="ship", blueprint="t", repo="o/r")
    await ex.execute(_bp(), task)

    assert task.state == TaskState.STAGE_FAILED
    assert llm.calls == []  # recovery never consulted
