"""Run-event spine tests — the live-observability derivations.

The event hub (PipelineService._on_event) derives per-agent status,
supervisor/orchestrator synthesis, and coordination A2A from every stage
event, fans them out to SSE/ring, and appends them to durable Redis logs.
These tests pin the derivation contract the dashboard depends on.
"""

from __future__ import annotations

import pytest

from devai.pipeline.service import PipelineService
from devai.pipeline.types import DevAITask, StageEvent, StageEventPhase, TaskState


class _Cfg:
    pipeline_label = "x"
    pipeline_event_ring_size = 100
    pipeline_task_ttl = 3600
    pipeline_blueprint_dir = "blueprints"


def _svc() -> PipelineService:
    return PipelineService(_Cfg())


def _event(stage="implement-code", phase=StageEventPhase.STARTED, agent="senior_developer", **kw) -> StageEvent:
    return StageEvent(stage, phase, agent=agent, lane="build", stage_type="agentic", **kw)


def _task(**kw) -> DevAITask:
    task = DevAITask(intent="ship it", blueprint="alm-pipeline", repo="org/app", **kw)
    task.state = TaskState.IMPLEMENTING if hasattr(TaskState, "IMPLEMENTING") else task.state
    return task


# ──────────────────────────────────────────────────────────────────────
# StageEvent carries agent identity
# ──────────────────────────────────────────────────────────────────────


def test_stage_event_agent_lane_roundtrip():
    e = _event()
    d = e.to_dict()
    assert d["agent"] == "senior_developer"
    assert d["lane"] == "build"
    back = StageEvent.from_dict(d)
    assert back.agent == "senior_developer"
    assert back.lane == "build"


def test_stage_event_from_dict_tolerates_old_snapshots():
    # Snapshots persisted before the agent/lane fields existed must load.
    old = {"stage": "x", "phase": "completed", "timestamp": 1.0}
    e = StageEvent.from_dict(old)
    assert e.agent == ""
    assert e.lane == ""


# ──────────────────────────────────────────────────────────────────────
# Hub derivations: agent status
# ──────────────────────────────────────────────────────────────────────


def test_started_event_marks_agent_running():
    svc, task = _svc(), _task()
    envelopes = svc._derive_run_signals(task, _event(), ts=100.0)

    assert task.agents["senior_developer"]["status"] == "running"
    assert task.agents["senior_developer"]["stage"] == "implement-code"
    statuses = [e for e in envelopes if e["event_type"] == "agent_status"]
    assert {"senior_developer", "supervisor", "orchestrator"} <= {e["agent"] for e in statuses}
    assert task.agents["supervisor"]["status"] == "running"


def test_completed_and_failed_events_update_agent_status():
    svc, task = _svc(), _task()
    svc._derive_run_signals(task, _event(), ts=100.0)
    svc._derive_run_signals(task, _event(phase=StageEventPhase.COMPLETED, duration_ms=1500), ts=101.0)
    assert task.agents["senior_developer"]["status"] == "completed"

    svc._derive_run_signals(
        task, _event(stage="run-tests", agent="qa_tester", phase=StageEventPhase.FAILED, error="2 tests red"), ts=102.0
    )
    assert task.agents["qa_tester"]["status"] == "failed"
    assert task.agents["qa_tester"]["error"] == "2 tests red"


def test_deterministic_stage_without_agent_emits_no_agent_or_a2a():
    svc, task = _svc(), _task()
    envelopes = svc._derive_run_signals(task, _event(stage="create-issue", agent=""), ts=100.0)
    agent_envs = [e for e in envelopes if e["event_type"] == "agent_status"]
    # Only the coordination layer lights up.
    assert {e["agent"] for e in agent_envs} == {"supervisor", "orchestrator"}
    assert not [e for e in envelopes if e["event_type"] == "a2a"]
    assert task.agent_context.get("a2a_messages") is None


def test_terminal_task_marks_coordinators_done_or_failed():
    svc, task = _svc(), _task()
    task.transition(TaskState.COMPLETED) if hasattr(task, "transition") else None
    task.state = TaskState.COMPLETED
    svc._derive_run_signals(task, _event(phase=StageEventPhase.COMPLETED), ts=100.0)
    assert task.agents["supervisor"]["status"] == "completed"

    task2 = _task()
    task2.state = TaskState.STAGE_FAILED
    svc._derive_run_signals(task2, _event(phase=StageEventPhase.FAILED, error="boom"), ts=100.0)
    assert task2.agents["supervisor"]["status"] == "failed"


# ──────────────────────────────────────────────────────────────────────
# Hub derivations: coordination A2A
# ──────────────────────────────────────────────────────────────────────


def test_a2a_handoff_response_escalation_lifecycle():
    svc, task = _svc(), _task()
    svc._derive_run_signals(task, _event(), ts=100.0)
    svc._derive_run_signals(task, _event(phase=StageEventPhase.COMPLETED, duration_ms=2000, message="done"), ts=101.0)
    svc._derive_run_signals(
        task, _event(stage="run-tests", agent="qa_tester", phase=StageEventPhase.FAILED, error="red"), ts=102.0
    )

    msgs = task.agent_context["a2a_messages"]
    assert [m["message_type"] for m in msgs] == ["handoff", "response", "escalation"]
    handoff, response, escalation = msgs
    assert handoff["from_agent"] == "supervisor" and handoff["to_agent"] == "senior_developer"
    assert response["from_agent"] == "senior_developer" and response["to_agent"] == "supervisor"
    assert "2.0s" in response["subject"]
    assert escalation["from_agent"] == "qa_tester"
    assert escalation["body"] == "red"
    # Every message has the canonical A2A shape the feed renders.
    for m in msgs:
        assert m["id"] and m["timestamp"] and m["trace_id"]


def test_a2a_list_is_capped():
    svc, task = _svc(), _task()
    task.agent_context["a2a_messages"] = [{"id": str(i)} for i in range(svc._A2A_CAP)]
    svc._derive_run_signals(task, _event(), ts=100.0)
    msgs = task.agent_context["a2a_messages"]
    assert len(msgs) == svc._A2A_CAP
    assert msgs[-1]["message_type"] == "handoff"  # newest kept, oldest dropped


def test_skipped_stage_produces_no_a2a():
    svc, task = _svc(), _task()
    envelopes = svc._derive_run_signals(task, _event(phase=StageEventPhase.SKIPPED), ts=100.0)
    assert not [e for e in envelopes if e["event_type"] == "a2a"]
    assert task.agents["senior_developer"]["status"] == "skipped"


# ──────────────────────────────────────────────────────────────────────
# Hub derivations: orchestrator routing / progress
# ──────────────────────────────────────────────────────────────────────


def test_orchestrator_routing_progress_and_phase():
    svc, task = _svc(), _task()
    task.stages_completed = ["a", "b"]
    task.current_stage = "implement-code"
    svc._derive_run_signals(task, _event(), ts=100.0)
    routing = task.agent_context["orchestrator_routing"]
    assert 0 < routing["progress_pct"] <= 100
    assert routing["current_phase"] == "build"
    assert "implement-code" in routing["status_summary"]


# ──────────────────────────────────────────────────────────────────────
# _on_event end-to-end: ring + SSE queues + task serialization
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_on_event_fans_out_stage_and_derived_envelopes():
    import asyncio

    svc, task = _svc(), _task()
    queue: asyncio.Queue = asyncio.Queue()
    svc._sse_queues.append(queue)

    svc._on_event(task, _event())

    kinds = []
    while not queue.empty():
        _, tid, payload = queue.get_nowait()
        assert tid == task.id
        kinds.append(payload["event_type"])
    assert "stage" in kinds
    assert "agent_status" in kinds
    assert "a2a" in kinds
    # Ring carries the same envelopes for SSE replay.
    ring_kinds = {p["event_type"] for _, _, p in svc._ring}
    assert {"stage", "agent_status", "a2a"} <= ring_kinds


def test_task_to_dict_roundtrips_agents():
    task = _task()
    task.agents["senior_developer"] = {"status": "running", "updated_at": 1.0, "stage": "implement-code"}
    d = task.to_dict()
    assert d["agents"]["senior_developer"]["status"] == "running"
    back = DevAITask.from_dict(d)
    assert back.agents["senior_developer"]["status"] == "running"


# ──────────────────────────────────────────────────────────────────────
# Durable Redis log writer
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_persist_run_log_appends_capped_ttl_lists():
    captured: list[tuple] = []

    class _Pipe:
        def rpush(self, key, value):
            captured.append(("rpush", key, value))

        def ltrim(self, key, start, end):
            captured.append(("ltrim", key, start, end))

        def expire(self, key, ttl):
            captured.append(("expire", key, ttl))

        async def execute(self):
            captured.append(("execute",))

    class _Redis:
        def pipeline(self):
            return _Pipe()

    class _SM:
        redis = _Redis()

    svc = _svc()
    svc.state_manager = _SM()
    envelopes = [
        {"event_type": "stage", "stage": "x", "phase": "started"},
        {"event_type": "a2a", "id": "m1", "from_agent": "supervisor", "to_agent": "dev"},
    ]
    svc._persist_run_log("task-1", envelopes, ts=100.0)
    # Let the spawned writer run.
    import asyncio

    await asyncio.sleep(0.01)

    keys = {c[1] for c in captured if c[0] == "rpush"}
    assert keys == {"devai:run:task-1:events", "devai:run:task-1:a2a_messages"}
    assert ("execute",) in captured
    ttls = [c for c in captured if c[0] == "expire"]
    assert all(c[2] == 3600 for c in ttls)
    # A2A entry stored without the envelope discriminator.
    a2a_raw = next(c[2] for c in captured if c[0] == "rpush" and "a2a" in c[1])
    assert "event_type" not in a2a_raw


# ──────────────────────────────────────────────────────────────────────
# AgentAdapter surfaces real A2A from legacy agents
# ──────────────────────────────────────────────────────────────────────


# ──────────────────────────────────────────────────────────────────────
# Epic supervision — milestone comments + terminal labels on the epic
# ──────────────────────────────────────────────────────────────────────


class _RecordingSCM:
    def __init__(self):
        self.comments: list[tuple[str, int, str]] = []
        self.labels: list[tuple[str, int, list[str]]] = []

    async def add_comment(self, repo, issue_id, body):
        self.comments.append((repo, issue_id, body))
        return {"id": 1}

    async def add_labels(self, repo, issue_id, labels):
        self.labels.append((repo, issue_id, list(labels)))


@pytest.mark.asyncio
async def test_epic_progress_posts_milestones_and_terminal_summary():
    import asyncio

    svc = _svc()
    scm = _RecordingSCM()
    svc.scm = scm
    task = _task()
    task.epic_issue_number = 42
    task.pr_number = 7

    # Milestone completion → one comment with agent + duration + PR ref.
    svc._epic_progress(task, _event(phase=StageEventPhase.COMPLETED, duration_ms=142500, message="PR ready"))
    # Non-milestone stage → no comment.
    svc._epic_progress(task, _event(stage="hydrate-context", phase=StageEventPhase.COMPLETED))
    # Duplicate of the same milestone (resume) → no second comment.
    svc._epic_progress(task, _event(phase=StageEventPhase.COMPLETED, duration_ms=142500))
    # Terminal summary + status label.
    task.state = TaskState.COMPLETED
    task.stages_completed = ["a", "b", "c"]
    svc._epic_progress(task, _event(stage="cleanup", phase=StageEventPhase.COMPLETED))
    await asyncio.sleep(0.02)  # let the spawned posts run

    assert len(scm.comments) == 2
    milestone, terminal = scm.comments
    assert milestone[1] == 42
    assert "Implement Code" in milestone[2]
    assert "senior_developer" in milestone[2]
    assert "142.5s" in milestone[2]
    assert "pull request #7" in milestone[2]
    assert "Pipeline run ✅ completed" in terminal[2]
    assert "#7" in terminal[2]
    assert scm.labels == [("org/app", 42, ["devai:done"])]


@pytest.mark.asyncio
async def test_epic_progress_skips_without_epic_or_on_dry_run():
    import asyncio

    svc = _svc()
    scm = _RecordingSCM()
    svc.scm = scm
    task = _task()  # no epic_issue_number
    svc._epic_progress(task, _event(phase=StageEventPhase.COMPLETED))
    task.epic_issue_number = 42
    task.dry_run = True
    svc._epic_progress(task, _event(phase=StageEventPhase.COMPLETED))
    await asyncio.sleep(0.01)
    assert scm.comments == []


# ──────────────────────────────────────────────────────────────────────
# Shared-queue claim guard — wrong service releases, never strands
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_worker_releases_unknown_blueprint_instead_of_crashing():
    """devai-api and devai-sre share devai:pipeline:queue but load different
    blueprint sets. A claimed task this service can't run must go BACK on the
    queue (for the right service) — the old behavior crashed on the lookup,
    acked, and stranded the run in `pending` forever."""
    import asyncio

    from devai.pipeline.interfaces import StageDeps
    from devai.pipeline.pipeline import Pipeline

    released: list[str] = []
    acked: list[str] = []
    task_dict = DevAITask(intent="x", blueprint="alm-pipeline", repo="org/app").to_dict()

    class _SM:
        async def claim_next_task(self, worker_id, *, claim_ttl=180):
            if released:  # second pass: stop the loop
                raise asyncio.CancelledError
            return task_dict["id"]

        async def release_task(self, task_id):
            released.append(task_id)
            return len(released)

        async def ack_task(self, task_id):
            acked.append(task_id)

        async def get_pipeline_task(self, task_id):
            return task_dict

        async def heartbeat_task(self, *a, **kw):
            return None

        async def persist_task(self, *a, **kw):
            return None

    class _Cfg2(_Cfg):
        pipeline_durable_queue = True
        pipeline_queue_poll_interval = 0.01

    deps = StageDeps(config=_Cfg2(), state_manager=_SM())
    pipe = Pipeline(deps)  # no blueprints registered — simulates the SRE pod
    assert pipe._durable

    with pytest.raises(asyncio.CancelledError):
        await pipe._durable_worker_loop(0)

    assert released == [task_dict["id"]]
    assert acked == []  # never acked — stays available for the right service


@pytest.mark.asyncio
async def test_worker_finalizes_stopped_run_instead_of_resurrecting():
    """A stopped run must NEVER be resurrected by a requeue path. The stop
    flag is checked at the claim choke point: the worker finalizes CANCELLED
    and acks instead of executing — even when a stale snapshot write made
    the run look non-terminal again (the zombie-writer race during a pod
    roll that re-ran a cancelled run in prod)."""
    import asyncio

    from devai.pipeline.interfaces import StageDeps
    from devai.pipeline.pipeline import Pipeline

    # Snapshot looks RUNNING (the stale overwrite) but control says stopped.
    task_obj = DevAITask(intent="x", blueprint="bp", repo="org/app")
    task_obj.state = TaskState.IMPLEMENTING
    task_dict = task_obj.to_dict()
    acked: list[str] = []
    persisted: list[dict] = []
    executed = {"flag": False}

    class _SM:
        async def claim_next_task(self, worker_id, *, claim_ttl=180):
            if acked:
                raise asyncio.CancelledError
            return task_dict["id"]

        async def get_pipeline_control(self, task_id):
            return "stopped"

        async def ack_task(self, task_id):
            acked.append(task_id)

        async def get_pipeline_task(self, task_id):
            return task_dict

        async def persist_task(self, d, **kw):
            persisted.append(d)

        async def heartbeat_task(self, *a, **kw):
            return None

        async def release_task(self, task_id):
            return 1

    class _Cfg2(_Cfg):
        pipeline_durable_queue = True
        pipeline_queue_poll_interval = 0.01

    deps = StageDeps(config=_Cfg2(), state_manager=_SM())
    pipe = Pipeline(deps)

    # Register the blueprint so the claim guard passes and ONLY the stop
    # guard can prevent execution.
    class _BP:
        name = "bp"
        stages = []

    pipe._blueprints["bp"] = _BP()

    async def _explode(*a, **kw):
        executed["flag"] = True

    pipe._execute_task = _explode  # type: ignore[method-assign]

    with pytest.raises(asyncio.CancelledError):
        await pipe._durable_worker_loop(0)

    assert executed["flag"] is False  # never re-executed
    assert acked == [task_dict["id"]]
    assert persisted and persisted[-1]["state"] == "cancelled"
    assert persisted[-1]["error"] == "stopped by user"


@pytest.mark.asyncio
async def test_persist_task_never_regresses_terminal_state():
    fakeredis = pytest.importorskip("fakeredis")
    from devai.core.state import StateManager

    sm = StateManager.__new__(StateManager)
    sm.redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    sm.result_ttl = 3600

    task = DevAITask(intent="x", blueprint="bp", repo="org/app")
    task.state = TaskState.CANCELLED
    terminal = task.to_dict()
    await sm.persist_task(terminal)

    # A zombie writer with a NEWER timestamp but non-terminal state loses.
    zombie = dict(terminal)
    zombie["state"] = "implementing"
    zombie["updated_at"] = terminal["updated_at"] + 100
    await sm.persist_task(zombie)
    stored = await sm.get_pipeline_task(task.id)
    assert stored["state"] == "cancelled"

    # But terminal→terminal updates still apply.
    done = dict(terminal)
    done["state"] = "completed"
    done["updated_at"] = terminal["updated_at"] + 200
    await sm.persist_task(done)
    stored = await sm.get_pipeline_task(task.id)
    assert stored["state"] == "completed"


@pytest.mark.asyncio
async def test_execute_task_unknown_blueprint_fails_visibly():
    from devai.pipeline.interfaces import StageDeps
    from devai.pipeline.pipeline import Pipeline

    persisted: list[dict] = []

    class _SM:
        async def claim_next_task(self, *a, **kw):
            return None

        async def persist_task(self, d, **kw):
            persisted.append(d)

    deps = StageDeps(config=_Cfg(), state_manager=_SM())
    pipe = Pipeline(deps)
    task = DevAITask(intent="x", blueprint="ghost", repo="org/app")
    pipe._tasks[task.id] = task
    await pipe._execute_task(task)
    assert task.state == TaskState.STAGE_FAILED
    assert "unknown blueprint" in (task.error or "")
    assert persisted  # failure state persisted, not lost


# ──────────────────────────────────────────────────────────────────────
# Gate pending semantics — only prompt when the run reached the gate
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gates_not_pending_before_run_reaches_them():
    svc = _svc()

    class _Stage:
        def __init__(self, name, gate=False):
            self.name = name
            self.gate = gate
            self.lane = "deploy"
            self.type = "review"

        def display_title(self):
            return self.name.title()

        def resolved_agent(self):
            return "staff_reviewer"

    class _BP:
        stages = [_Stage("implement"), _Stage("staff-review", gate=True), _Stage("deploy-release", gate=True)]

    class _Pipe:
        def get_blueprint(self, name):
            return _BP()

    svc._pipeline = _Pipe()
    svc._started = True

    fresh = DevAITask(intent="ship", blueprint="alm-pipeline", repo="org/app").to_dict()

    async def fake_get_task(task_id):
        return fresh

    svc.get_task = fake_get_task  # type: ignore[method-assign]

    gates = await svc.list_gates("t1")
    assert [g["gate"] for g in gates] == ["staff-review", "deploy-release"]
    # Run hasn't started → NOTHING is pending (the old logic prompted both).
    assert all(g["pending"] is False for g in gates)

    # Now the run reaches staff-review: STARTED event + current_stage.
    fresh["current_stage"] = "staff-review"
    fresh["stage_events"] = [{"stage": "staff-review", "phase": "started", "timestamp": 1.0, "message": "review ok"}]
    fresh["stages_completed"] = ["implement"]
    gates = await svc.list_gates("t1")
    by_name = {g["gate"]: g for g in gates}
    assert by_name["staff-review"]["pending"] is True
    assert by_name["deploy-release"]["pending"] is False  # still not reached
    # Context the banner renders:
    assert by_name["staff-review"]["agent"] == "staff_reviewer"
    assert by_name["staff-review"]["requested_at"] == 1.0
    assert by_name["staff-review"]["summary"] == "review ok"


def test_agent_adapter_build_result_surfaces_a2a():
    from devai.pipeline.interfaces import StageDeps
    from devai.pipeline.stages._base import AgentAdapter

    class _Adapter(AgentAdapter):
        def name(self):
            return "x"

        def role_key(self):
            return "senior_developer"

        def _make_agent(self):
            return None

    adapter = _Adapter(StageDeps(config=_Cfg()), {})
    task = _task()
    msgs = [{"id": "1", "from_agent": "senior_developer", "to_agent": "qa", "message_type": "request"}]
    result = adapter._build_result(task, {"summary": "ok", "a2a_messages": msgs})
    assert result.data["a2a_messages"] == msgs
