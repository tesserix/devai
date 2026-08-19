"""Blueprint executor — runs a Blueprint against a DevAITask.

Mirrors `internal/blueprint/executor.go`. The executor:

1. Topo-sorts the stages into levels (Kahn's algorithm).
2. Walks each level; stages within a level with `parallel: true` run via
   `asyncio.gather`, otherwise sequential.
3. Evaluates `condition:` per stage (skip if False).
4. Applies per-stage `timeout` via `asyncio.wait_for`.
5. Emits StageEvent(started|completed|failed|skipped) for every transition.
6. Handles `on_failure: stop | rollback | continue`.
7. Honors `shouldRunStageFromState` for resumption — already-completed
   stages on `task.stages_completed` are skipped on replay.

The executor is intentionally state-machine-agnostic. It records what
happened; the Pipeline class on top decides what state to transition the
task into based on the StageResult.next_state hint.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re as _re
import time
from collections.abc import Callable, Iterable
from typing import Any

from devai.blueprint.conditions import evaluate as eval_condition
from devai.blueprint.loader import Blueprint, StageSpec
from devai.blueprint.planner import topological_levels
from devai.blueprint.registry import StageRegistry, StageRegistryError
from devai.pipeline.interfaces import PipelineStage, StageDeps
from devai.pipeline.types import (
    DevAITask,
    StageEvent,
    StageEventPhase,
    StageResult,
    TaskState,
)

logger = logging.getLogger(__name__)


class BlueprintExecutionError(Exception):
    """Raised when a blueprint can't be executed (cycle, missing stage, ...)."""


class _RunStopped(Exception):
    """Internal: the user stopped the run while a stage was executing."""


# Hook for streaming events to subscribers (SSE, audit log, JetStream
# bridge). Called synchronously from the executor loop with the
# in-progress task and the freshly-emitted event.
EventCallback = Callable[[DevAITask, StageEvent], None]


# Exception strings can embed live credentials — DB DSNs with passwords,
# provider API keys, bearer tokens. The recovery agent posts error text to
# GitHub issues (potentially PUBLIC repos) and feeds it to LLM prompts, so
# everything leaving the process goes through this scrub first.
_SECRET_PATTERNS = [
    _re.compile(r"(://[^/\s:@]+:)[^@\s]+(@)"),  # scheme://user:PASS@host
    _re.compile(r"\b(sk-[A-Za-z0-9_-]{8,})"),
    _re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{20,})"),
    _re.compile(r"\b(github_pat_[A-Za-z0-9_]{20,})"),
    _re.compile(r"\b(AKIA[A-Z0-9]{12,})"),
    _re.compile(r"\b(xox[baprs]-[A-Za-z0-9-]{10,})"),
    _re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{16,}"),
    _re.compile(r"(?i)\b((?:api[_-]?key|token|password|secret)\s*[=:]\s*)[^\s&\"']{6,}"),
]


def _redact(text: str) -> str:
    """Mask credential-shaped substrings before text leaves the process."""
    out = text or ""
    for pat in _SECRET_PATTERNS:
        out = pat.sub(
            lambda m: (
                (m.group(1) if m.lastindex else "") + "***REDACTED***" + (m.group(2) if (m.lastindex or 0) >= 2 else "")
            ),
            out,
        )
    return out


def _parse_json_lenient(text: str) -> Any:
    """Parse LLM JSON tolerating markdown code fences."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return json.loads(t.strip())


def _otel_stage_span(spec: StageSpec, task: DevAITask) -> Any:
    """OTel span for one stage attempt — the agent-native trace plane.

    Names the span after the agent persona (``agent.senior_developer``) so a
    run reads as a tree of agent spans in Tempo/Grafana; the LLM calls inside
    nest automatically through OTel's contextvar propagation. No-op (a plain
    null context) whenever telemetry is the Noop.
    """
    try:
        from devai.adapters.telemetry.runtime import get_global_telemetry

        agent = spec.resolved_agent() or spec.name
        return get_global_telemetry().span(
            f"agent.{agent}",
            attributes={
                "devai.run_id": task.id,
                "devai.blueprint": task.blueprint or "",
                "devai.stage": spec.name,
                "devai.stage_handler": spec.stage,
                "devai.agent": agent,
                "devai.lane": spec.lane or "",
                "devai.repo": task.repo or "",
                "devai.triggered_by": task.triggered_by or "",
            },
        )
    except Exception:  # noqa: BLE001 — tracing must never break a stage
        import contextlib as _c

        return _c.nullcontext(None)


def _stage_trace(spec: StageSpec, task: DevAITask) -> Any:
    """LangSmith trace context for one stage attempt; no-op when disabled.

    The legacy LangGraph agents were traced via @traceable; the blueprint
    executor calls LLM adapters directly, so without this the new pipeline
    was invisible in LangSmith ("traces are all missing").
    """
    try:
        from devai.services.tracing import is_tracing_enabled

        if not is_tracing_enabled():
            return contextlib.nullcontext(None)
        from langsmith.run_helpers import trace

        return trace(
            name=f"stage:{spec.name}",
            run_type="chain",
            inputs={"intent": (task.intent or "")[:500], "handler": spec.stage},
            metadata={
                "run_id": task.id,
                "blueprint": task.blueprint or "",
                "repo": task.repo or "",
                "agent": spec.resolved_agent() or "",
                "triggered_by": task.triggered_by or "",
            },
        )
    except Exception:  # noqa: BLE001 — tracing must never break a stage
        return contextlib.nullcontext(None)


# Strong refs for fire-and-forget eval writes (asyncio only weak-refs tasks).
_EVAL_TASKS: set[Any] = set()


def _record_stage_evals(spec: StageSpec, task: DevAITask, data: dict[str, Any] | None) -> None:
    """Persist quality-gate outcomes from a stage's handover as eval rows.

    review_decision / security_decision / test counts / build_status map to
    0..1 scores in agent_evals — the analytics quality view aggregates them.
    Fire-and-forget: scoring must never slow or break the run.
    """
    if not data:
        return
    evals: list[tuple[str, float, bool]] = []  # (evaluator, score, passed)
    review = str(data.get("review_decision") or "").lower()
    if review:
        ok = review in ("approve", "approved", "pass")
        evals.append(("review", 1.0 if ok else 0.0, ok))
    security = str(data.get("security_decision") or "").lower()
    if security:
        ok = security in ("approve", "approved", "pass", "passed")
        evals.append(("security", 1.0 if ok else 0.0, ok))
    total = data.get("test_total")
    if isinstance(total, int) and total > 0:
        passed_n = int(data.get("test_passed") or 0)
        evals.append(("tests", passed_n / total, int(data.get("test_failed") or 0) == 0))
    build = str(data.get("build_status") or "").lower()
    if build:
        ok = build in ("success", "passed", "green")
        evals.append(("build", 1.0 if ok else 0.0, ok))
    if not evals:
        return

    async def _write() -> None:
        try:
            from devai.services.database import get_global_db

            db = await get_global_db()
            if db is None:
                return
            principal = task.principal or {}
            for evaluator, score, passed in evals:
                await db.record_eval(
                    run_id=task.id,
                    evaluator=evaluator,
                    score=score,
                    passed=passed,
                    stage=spec.name,
                    agent_name=spec.resolved_agent() or "",
                    triggered_by=task.triggered_by or "",
                    tenant_id=str(principal.get("tenant_id") or ""),
                    user_id=str(principal.get("uid") or task.triggered_by or ""),
                )
        except Exception:  # noqa: BLE001 — eval capture is best-effort
            logger.debug("eval persistence failed for stage %s", spec.name, exc_info=True)

    try:
        t = asyncio.get_running_loop().create_task(_write())
        _EVAL_TASKS.add(t)
        t.add_done_callback(_EVAL_TASKS.discard)
    except RuntimeError:
        pass  # no running loop (sync test context)


def _finalize_agent_statuses(task: DevAITask, status: str = "cancelled") -> None:
    """Close out lingering 'running' agent statuses when a run stops — the
    cards must never pulse on a terminal run."""
    for entry in task.agents.values():
        if isinstance(entry, dict) and entry.get("status") in ("running", "in_progress"):
            entry["status"] = status
            entry["updated_at"] = time.time()


class BlueprintExecutor:
    """Runs a Blueprint against a DevAITask.

    Stateless across runs — instantiate once per Pipeline and reuse.
    `execute()` is async and yields control between stages so the
    Pipeline can run multiple tasks concurrently.
    """

    def __init__(
        self,
        registry: StageRegistry,
        deps: StageDeps,
        *,
        event_callback: EventCallback | None = None,
        default_stage_timeout: float = 900.0,
    ) -> None:
        self._registry = registry
        self._deps = deps
        self._event_cb = event_callback
        self._default_timeout = default_stage_timeout
        # Global transient-retry default; per-stage `retries:` overrides.
        self._default_retries = max(0, int(getattr(deps.config, "pipeline_stage_retries", 1) or 0))
        # Autonomy default for gate stages: "full" self-approves gates so
        # runs flow end-to-end; "gated" pauses for a human decision.
        self._default_autonomy = str(getattr(deps.config, "pipeline_default_autonomy", "full") or "full").lower()
        # Autonomous failure recovery: rounds the recovery agent gets per
        # stage AFTER transient retries exhaust, before on_failure applies.
        heal_enabled = bool(getattr(deps.config, "pipeline_heal_on_failure", True))
        self._heal_rounds = (
            min(5, max(0, int(getattr(deps.config, "pipeline_heal_attempts", 3) or 0))) if heal_enabled else 0
        )

    async def execute(self, blueprint: Blueprint, task: DevAITask) -> DevAITask:
        """Execute every stage in topo order. Returns the (mutated) task.

        Failure semantics:
            on_failure=stop      — short-circuit; task → STAGE_FAILED.
            on_failure=rollback  — invoke stage.rollback(), then stop.
            on_failure=continue  — record failure, keep going.
        """
        levels = _topo_sort(blueprint.stages)
        logger.info(
            "executing blueprint %s on task %s — %d stages in %d levels",
            blueprint.name,
            task.id,
            len(blueprint.stages),
            len(levels),
        )

        # Mark the run ACTIVE the moment execution begins. Without this the task
        # sits at QUEUED for the entire blueprint: the app-scaffold run_as_job
        # stages (scan/install_deps/seed_mocks) carry no `next_state`, so nothing
        # moves it off QUEUED even as stages clearly run. The dashboard then reads
        # state=queued, concludes the run hasn't started, and shows the QUEUED
        # badge + "Waiting for the agent…" with empty Logs/Events/Timeline even
        # though stage events are streaming. Per-stage sub-states (PLANNING/
        # IMPLEMENTING) still refine this; we only guarantee it leaves QUEUED.
        if task.state in (TaskState.PENDING, TaskState.QUEUED):
            task.transition(TaskState.RUNNING)
            if task.started_at is None:
                task.started_at = time.time()

        for level_idx, level in enumerate(levels):
            # Honor user run-control (pause / stop) at each stage boundary.
            # Returns False when the run was stopped (task already CANCELLED).
            if not await self._check_run_control(task):
                logger.info("blueprint %s stopped by user at level %d", blueprint.name, level_idx)
                break

            # Group stages within a level: parallel-marked stages all run
            # together, sequential stages run one at a time. In practice we
            # keep this simple — if every stage in a level has parallel=True
            # we gather them; otherwise we run sequentially within the level.
            parallel_in_level = [s for s in level if s.parallel]
            sequential_in_level = [s for s in level if not s.parallel]

            if parallel_in_level and not sequential_in_level:
                # All-parallel level
                await asyncio.gather(*(self._run_one(spec, task) for spec in parallel_in_level))
            else:
                # Mixed or all-sequential
                for spec in level:
                    await self._run_one(spec, task)

            if task.is_failed:
                logger.warning("blueprint %s halted at level %d due to failure", blueprint.name, level_idx)
                break

        if not task.is_failed and not task.is_terminal:
            task.transition(TaskState.COMPLETED)
        return task

    # ──────────────────────────────────────────────────────────────────
    # Internal: run control (pause / stop)
    # ──────────────────────────────────────────────────────────────────

    _CONTROL_POLL_SECONDS = 2.0
    _CONTROL_MAX_PAUSE_SECONDS = 3600.0

    async def _check_run_control(self, task: DevAITask) -> bool:
        """Poll the durable run-control flag at a stage boundary.

        Returns False if the run was stopped (and transitions the task to
        CANCELLED); blocks while paused (up to a max), then resumes. A no-op
        returning True when no StateManager exposes the control surface, so
        tests and minimal deps are unaffected. The Temporal workflow uses
        Signals instead — this drives the in-process path only.
        """
        sm = self._deps.state_manager
        getter = getattr(sm, "get_pipeline_control", None)
        if sm is None or getter is None:
            return True
        waited = 0.0
        while True:
            try:
                ctrl = await getter(task.id)
            except Exception:  # noqa: BLE001 — control is best-effort, never fatal
                return True
            if ctrl == "stopped":
                task.error = "stopped by user"
                task.failed_stage = task.current_stage or ""
                task.current_stage = ""
                _finalize_agent_statuses(task)
                task.transition(TaskState.CANCELLED)
                return False
            if ctrl == "paused" and waited < self._CONTROL_MAX_PAUSE_SECONDS:
                await asyncio.sleep(self._CONTROL_POLL_SECONDS)
                waited += self._CONTROL_POLL_SECONDS
                continue
            return True

    # ──────────────────────────────────────────────────────────────────
    # Internal: stage execution
    # ──────────────────────────────────────────────────────────────────

    async def _run_one(self, spec: StageSpec, task: DevAITask) -> None:
        """Execute one stage spec, handling all gates (skip / condition /
        already-done / timeout / on_failure)."""

        # One factory for every event this stage emits so each carries the
        # agent persona + lane — the dashboard lights up the matching agent
        # card / phase group straight from the event, no blueprint re-read.
        def _ev(phase: StageEventPhase, **kw: Any) -> StageEvent:
            return StageEvent(
                spec.name,
                phase,
                stage_type=spec.type,
                gate=spec.gate,
                agent=spec.resolved_agent(),
                lane=spec.lane,
                **kw,
            )

        # Resumption: already done on a previous run.
        if spec.name in task.stages_completed:
            logger.debug("stage %s already completed — skipping", spec.name)
            self._emit(task, _ev(StageEventPhase.SKIPPED, message="already completed"))
            return

        # Condition gate.
        try:
            should_run = eval_condition(spec.condition, task)
        except ValueError as e:
            logger.error("stage %s: bad condition %r: %s", spec.name, spec.condition, e)
            task.stages_failed.append(spec.name)
            task.error = f"bad condition on {spec.name}: {e}"
            task.failed_stage = spec.name
            task.transition(TaskState.STAGE_FAILED)
            self._emit(task, _ev(StageEventPhase.FAILED, error=str(e)))
            return

        if not should_run:
            logger.info("stage %s: condition %r evaluated False — skipping", spec.name, spec.condition)
            task.stages_skipped.append(spec.name)
            self._emit(task, _ev(StageEventPhase.SKIPPED, message=f"condition: {spec.condition}"))
            return

        # Resolve the stage instance. We stamp `__stage_name` into the
        # config so generic stage handlers (`run_as_job`, `noop`, etc.)
        # can recover the YAML stage name without parsing the blueprint.
        try:
            cfg_with_name = {**spec.config, "__stage_name": spec.name}
            stage = self._registry.resolve(spec.stage, self._deps, cfg_with_name)
        except StageRegistryError as e:
            task.stages_failed.append(spec.name)
            task.error = str(e)
            task.failed_stage = spec.name
            task.transition(TaskState.STAGE_FAILED)
            self._emit(task, _ev(StageEventPhase.FAILED, error=str(e)))
            return

        # Human approval gate. `gate: true` stages WAIT for a decision when
        # the run's autonomy mode asks for it; in full-autonomy mode (the
        # default) the gate self-approves with an audit trail and the run
        # flows end-to-end without prompting.
        if spec.gate and not await self._resolve_gate(spec, task, _ev):
            return  # rejected/stopped at the gate — task already transitioned

        # Run it.
        task.current_stage = spec.name
        start = time.monotonic()
        self._emit(task, _ev(StageEventPhase.STARTED, message=spec.stage))

        timeout = spec.timeout_seconds or self._default_timeout
        # Transient-failure resilience: exceptions/timeouts re-run the stage
        # (with linear backoff) before on_failure semantics apply. Stages are
        # resume-idempotent by design — a retry is no riskier than the
        # pod-restart resume path that already re-runs unfinished stages.
        max_attempts = 1 + (spec.retries if spec.retries > 0 else self._default_retries)
        heal_rounds_left = self._heal_rounds
        result: StageResult | object | None = None
        while True:
            outcome, payload = await self._attempt_with_retries(stage, spec, task, timeout, max_attempts, start, _ev)
            if outcome == "stopped":
                return
            if outcome == "ok":
                result = payload
                break
            # Retries exhausted — autonomous recovery BEFORE on_failure
            # semantics: a recovery agent reviews the failure, files/updates
            # the bug issue, injects corrective guidance, and the stage
            # re-runs. When recovery exhausts its rounds (or is rejected),
            # the runbook documents everything tried for the human.
            error, failure_state = payload
            if heal_rounds_left > 0 and await self._heal_stage(spec, task, str(error), _ev):
                heal_rounds_left -= 1
                max_attempts = 1  # one healed attempt per recovery round
                continue
            await self._write_runbook(spec, task, str(error))
            await self._handle_failure(
                spec, stage, task, str(error), failure_state, (time.monotonic() - start) * 1000.0
            )
            return

        duration_ms = (time.monotonic() - start) * 1000.0

        # Success path — merge handover and advance state.
        if not isinstance(result, StageResult):
            err = f"stage {spec.name} returned {type(result).__name__}, expected StageResult"
            await self._handle_failure(spec, stage, task, err, TaskState.STAGE_FAILED, duration_ms)
            return

        task.merge_handover(result.data)
        if result.next_state is not None:
            task.transition(result.next_state)
        task.stages_completed.append(spec.name)
        task.current_stage = ""
        # Quality-gate outcomes (review/security/tests/build) become eval rows
        # — the ONLY writer of agent_evals, feeding the analytics quality view.
        _record_stage_evals(spec, task, result.data)

        self._emit(task, _ev(StageEventPhase.COMPLETED, duration_ms=duration_ms, message=result.message))

    async def _execute_supervised(
        self, stage: PipelineStage, spec: StageSpec, task: DevAITask, timeout: float
    ) -> StageResult | object:
        """Run the stage while WATCHING the run-control flag and liveness.

        STOP must take effect mid-stage — agent stages run for many minutes
        and a user pressing Stop expects the run to halt now, not at the
        next level boundary. We race the stage against a periodic control
        poll: on `stopped` the in-flight stage task is cancelled and
        :class:`_RunStopped` raised.

        The timeout is PROGRESS-AWARE: a stage past its deadline whose agent
        shows recent tool activity (the tool layer heartbeats
        ``devai:run:<id>:activity``) is extended, up to ``timeout × hard cap
        multiplier`` — long builds finish; a stage that goes silent for the
        inactivity grace window dies at its deadline as before. Without a
        control surface (tests/minimal deps) this degrades to a plain
        wait_for.
        """
        # Turn-level observability: every LLM provider call made anywhere
        # under this stage inherits the run/agent context via contextvars
        # (set BEFORE create_task — task creation snapshots the context), so
        # per-turn envelopes (usage, narration, tool calls) land on the
        # run's event stream without the agents knowing about runs at all.
        from devai.services.agent_turns import reset_turn_context, set_turn_context, update_turn_context

        ctx_token = set_turn_context(task.id, spec.resolved_agent() or "", spec.name)
        principal = task.principal or {}
        update_turn_context(
            triggered_by=task.triggered_by or "",
            tenant_id=str(principal.get("tenant_id") or ""),
            user_id=str(principal.get("uid") or task.triggered_by or ""),
        )

        sm = self._deps.state_manager
        getter = getattr(sm, "get_pipeline_control", None) if sm is not None else None
        if getter is None:
            try:
                return await asyncio.wait_for(stage.execute(task), timeout=timeout)
            finally:
                reset_turn_context(ctx_token)

        grace = max(30.0, float(getattr(self._deps.config, "pipeline_stage_inactivity_grace", 240) or 240))
        hard_mult = max(1, int(getattr(self._deps.config, "pipeline_stage_hard_cap_multiplier", 4) or 4))

        exec_task = asyncio.create_task(stage.execute(task), name=f"stage-{spec.name}-{task.id}")
        deadline = time.monotonic() + timeout
        hard_deadline = time.monotonic() + timeout * hard_mult
        extended = False
        try:
            while True:
                done, _ = await asyncio.wait({exec_task}, timeout=self._CONTROL_POLL_SECONDS)
                if done:
                    return exec_task.result()  # re-raises stage exceptions naturally
                now = time.monotonic()
                if now >= deadline:
                    if now < hard_deadline and await self._recent_activity(task.id, grace):
                        # Actively working — extend in one-minute slices.
                        deadline = now + 60.0
                        if not extended:
                            extended = True
                            logger.info(
                                "stage %s past its %.0fs timeout but actively working — extending (hard cap %.0fs)",
                                spec.name,
                                timeout,
                                timeout * hard_mult,
                            )
                    else:
                        raise TimeoutError
                try:
                    if await getter(task.id) == "stopped":
                        raise _RunStopped
                except _RunStopped:
                    raise
                except Exception:  # noqa: BLE001 — control-read blip ≠ stop
                    continue
        finally:
            reset_turn_context(ctx_token)
            if not exec_task.done():
                exec_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await exec_task

    async def _recent_activity(self, task_id: str, grace_seconds: float) -> bool:
        """True when the agent's tool-layer heartbeat is fresher than the
        grace window — the liveness signal behind progress-aware timeouts."""
        sm = self._deps.state_manager
        redis = getattr(sm, "redis", None) if sm is not None else None
        if redis is None:
            return False
        try:
            ts = await redis.get(f"devai:run:{task_id}:activity")
            return ts is not None and (time.time() - float(ts)) < grace_seconds
        except Exception:  # noqa: BLE001 — liveness read failure ≠ alive
            return False

    async def _attempt_with_retries(
        self,
        stage: PipelineStage,
        spec: StageSpec,
        task: DevAITask,
        timeout: float,
        max_attempts: int,
        start: float,
        _ev: Any,
    ) -> tuple[str, Any]:
        """Run the stage with transient retries.

        Returns ``("ok", result)``, ``("stopped", None)`` (task already
        CANCELLED + event emitted), or ``("failed", (error, TaskState))``
        when every attempt failed — the caller decides recovery/on_failure.
        """
        for attempt in range(1, max_attempts + 1):
            try:
                # Two trace planes per attempt, both no-ops when off:
                #  - OTel span → the collector → Tempo/Grafana (infra-native);
                #  - LangSmith run → the LangSmith project (LLM-native).
                # Either way LLM calls inside inherit this parent through
                # contextvars, so a run reads as stage → llm-call trees.
                with _otel_stage_span(spec, task), _stage_trace(spec, task) as _rt:
                    result = await self._execute_supervised(stage, spec, task, timeout)
                    if _rt is not None and isinstance(result, StageResult):
                        with contextlib.suppress(Exception):
                            _rt.end(outputs={"message": result.message[:500]})
                    return ("ok", result)
            except _RunStopped:
                # User pressed STOP while the stage was executing. The old
                # behavior only honored stop at LEVEL boundaries, so a
                # long-running agent stage kept going for up to its full
                # timeout after the user stopped the run.
                duration_ms = (time.monotonic() - start) * 1000.0
                task.error = "stopped by user"
                task.failed_stage = spec.name
                task.current_stage = ""
                _finalize_agent_statuses(task)
                task.transition(TaskState.CANCELLED)
                self._emit(
                    task,
                    _ev(StageEventPhase.FAILED, duration_ms=duration_ms, error="stopped by user mid-stage"),
                )
                return ("stopped", None)
            except TimeoutError:
                err = f"timed out after {timeout}s"
                logger.error("stage %s: %s (attempt %d/%d)", spec.name, err, attempt, max_attempts)
                if attempt >= max_attempts:
                    return ("failed", (err, TaskState.AGENT_TIMEOUT))
            except Exception as e:  # noqa: BLE001 — we want to catch all stage errors
                logger.exception("stage %s raised (attempt %d/%d)", spec.name, attempt, max_attempts)
                if attempt >= max_attempts:
                    return ("failed", (str(e), TaskState.STAGE_FAILED))
            self._emit(
                task,
                _ev(
                    StageEventPhase.STARTED,
                    message=f"retrying after failure (attempt {attempt + 1}/{max_attempts})",
                ),
            )
            await asyncio.sleep(min(5.0 * attempt, 30.0))
        return ("failed", ("retries exhausted", TaskState.STAGE_FAILED))

    # ──────────────────────────────────────────────────────────────────
    # Internal: autonomous failure recovery
    # ──────────────────────────────────────────────────────────────────

    _HEAL_AGENT = "recovery_specialist"

    async def _heal_stage(self, spec: StageSpec, task: DevAITask, error: str, _ev: Any) -> bool:
        """A recovery agent reviews the failed stage and decides how to fix it.

        retry    — corrective guidance is injected into ``agent_context`` (the
                   AgentAdapter folds it into the stage's next prompt) and the
                   stage re-runs.
        ask_user — the proposed fix needs a human decision: a dynamic gate is
                   raised with the diagnosis + proposal; approval re-runs the
                   stage, rejection lets the failure stand.
        abort    — recovery impossible; the failure stands.

        Returns True when the caller should re-run the stage.
        """
        heal_stage = f"heal:{spec.name}"
        # Everything below (bug issue, comments, LLM prompts, events) may
        # leave the process — scrub credential-shaped substrings ONCE here.
        error = _redact(error)

        def _hev(phase: StageEventPhase, **kw: Any) -> StageEvent:
            return StageEvent(heal_stage, phase, stage_type="recovery", agent=self._HEAL_AGENT, lane=spec.lane, **kw)

        self._emit(
            task,
            _hev(
                StageEventPhase.STARTED,
                message=f"recovery agent reviewing failure of {spec.name}: {error[:160]}",
            ),
        )
        start = time.monotonic()
        decision = await self._diagnose_failure(spec, task, error)
        duration_ms = (time.monotonic() - start) * 1000.0

        llm = self._deps.llm
        llm_usable = llm is not None and getattr(llm, "provider_name", "noop") != "noop"
        if decision is None:
            # A flaky diagnosis call must never doom the recovery loop — the
            # user contract is "retry with injected context, minimum 3
            # rounds". Timeouts get the incremental-continuation brief (a
            # mechanical retry helps even with no LLM); any other failure
            # gets the error itself as corrective context — but ONLY when an
            # LLM actually exists. With a noop provider a deterministic
            # failure would just burn 3 mechanical re-runs and spam GitHub
            # bugs nobody is diagnosing.
            if "timed out" in error.lower():
                decision = {
                    "action": "retry",
                    "diagnosis": "stage exceeded its time budget before finishing",
                    "guidance": (
                        "Continue from the existing branch state. Do NOT recreate the "
                        "branch or re-commit files that already exist — list the branch "
                        "first, then finish ONLY the remaining work and open the PR."
                    ),
                }
            elif llm_usable:
                decision = {
                    "action": "retry",
                    "diagnosis": "automated diagnosis unavailable — retrying with the raw error as context",
                    "guidance": (
                        f"The previous attempt failed with: {error[:600]}\n"
                        "Avoid repeating the exact operation that produced this error; "
                        "work around it or use an alternative tool/approach."
                    ),
                }

        if decision is None or decision.get("action") == "abort":
            why = str((decision or {}).get("diagnosis") or "no usable diagnosis (LLM unavailable)")
            self._emit(
                task,
                _hev(StageEventPhase.FAILED, duration_ms=duration_ms, error=f"recovery abandoned: {why[:300]}"),
            )
            return False

        diagnosis = str(decision.get("diagnosis") or "")
        history = task.agent_context.setdefault("heal_history", [])
        if isinstance(history, list):
            history.append(
                {
                    "stage": spec.name,
                    "error": error[:500],
                    "diagnosis": diagnosis[:500],
                    "action": decision.get("action"),
                    "at": time.time(),
                }
            )
        prev_rec = task.agent_context.get(f"heal:{spec.name}") or {}
        task.agent_context[f"heal:{spec.name}"] = {
            # Bug-issue linkage survives across rounds.
            **{k: prev_rec[k] for k in ("bug_issue", "bug_url") if prev_rec.get(k)},
            "error": error[:800],
            "diagnosis": diagnosis[:1200],
            "guidance": str(decision.get("guidance") or "")[:2000],
        }

        # File/update the GitHub bug for this stage failure — the durable,
        # human-visible track of what broke and what the recovery agent is
        # doing about it. Best-effort: no SCM → recovery still proceeds.
        await self._heal_bug_tracker(spec, task, error, decision, _hev)

        autonomy = str(task.agent_context.get("autonomy") or self._default_autonomy).lower()
        if decision.get("action") == "ask_user" and autonomy != "full":
            if not await self._await_heal_approval(spec, task, decision, _hev):
                self._emit(
                    task,
                    _hev(StageEventPhase.FAILED, duration_ms=duration_ms, error="recovery plan rejected"),
                )
                return False

        self._emit(
            task,
            _hev(
                StageEventPhase.COMPLETED,
                duration_ms=duration_ms,
                message=f"recovery: re-running {spec.name} — {diagnosis[:200]}",
            ),
        )
        self._emit(task, _ev(StageEventPhase.STARTED, message="retrying with recovery guidance"))
        return True

    async def _heal_bug_tracker(
        self, spec: StageSpec, task: DevAITask, error: str, decision: dict[str, Any], _hev: Any
    ) -> None:
        """File (round 1) or update (rounds 2+) the GitHub bug issue tracking
        this stage failure — labeled, run-correlated, linked to the epic/PR,
        and updated with every recovery attempt's diagnosis + plan."""
        scm = self._deps.scm
        if scm is None or getattr(task, "dry_run", False) or not task.repo:
            return
        from devai.pipeline.stages._base import run_correlation_label

        rec = task.agent_context.get(f"heal:{spec.name}") or {}
        attempt_no = len([h for h in (task.agent_context.get("heal_history") or []) if h.get("stage") == spec.name])
        # Everything below lands on a PUBLIC issue and the error/diagnosis are
        # laundered from untrusted logs — strip @mentions and hidden HTML
        # comments so injected text can't page people or seed the next bot.
        from devai.services.prompt_guard import neutralize_for_issue

        body_core = neutralize_for_issue(
            f"### Recovery attempt {attempt_no}\n\n"
            f"**Error:**\n```\n{error[:800]}\n```\n\n"
            f"**Diagnosis:** {str(decision.get('diagnosis') or '')[:800]}\n\n"
            f"**Recovery action:** `{decision.get('action')}` — "
            f"{str(decision.get('guidance') or '')[:800]}"
        )
        try:
            bug = rec.get("bug_issue")
            if not bug:
                refs = []
                if task.epic_issue_number:
                    refs.append(f"**Epic:** #{task.epic_issue_number}")
                if task.pr_number:
                    refs.append(f"**Pull request:** #{task.pr_number}")
                issue = await scm.create_issue(
                    task.repo,
                    title=f"[bug] stage {spec.name} failed on run {task.id}: {error[:70]}",
                    body=(
                        "Automated stage-failure bug filed by the recovery agent.\n\n"
                        f"**Run:** `{task.id}`\n"
                        f"**Stage:** `{spec.name}` (agent: {spec.resolved_agent() or 'n/a'})\n"
                        + "\n".join(refs)
                        + f"\n\n{body_core}\n\n"
                        "_The recovery agent injects this diagnosis into the stage and retries; "
                        "every further attempt is recorded here. If recovery exhausts its rounds, "
                        "a runbook with everything tried is posted below._"
                    ),
                    labels=["bug", "devai:bug", "devai:stage-failure", run_correlation_label(task.id)],
                )
                rec["bug_issue"] = issue.get("number")
                rec["bug_url"] = issue.get("html_url", "")
                task.agent_context[f"heal:{spec.name}"] = rec
                self._emit(
                    task,
                    _hev(
                        StageEventPhase.STARTED,
                        message=f"recovery bug filed: #{rec['bug_issue']} {rec['bug_url']}",
                    ),
                )
            else:
                await scm.add_comment(task.repo, bug, body_core)
        except Exception:  # noqa: BLE001 — bug tracking must never break recovery
            logger.exception("heal bug tracking failed for stage %s", spec.name)

    async def _write_runbook(self, spec: StageSpec, task: DevAITask, error: str) -> None:
        """Recovery exhausted — post the runbook: full chronology of what was
        tried, the surviving error, and what a human should check next.
        Lands on the stage's bug issue (labeled devai:needs-human) and in
        agent_context['runbook:<stage>'] for the dashboard."""
        history = [h for h in (task.agent_context.get("heal_history") or []) if h.get("stage") == spec.name]
        if not history:
            return
        error = _redact(error)
        chrono = "\n".join(
            f"{i + 1}. **Error:** `{str(h.get('error') or '')[:140]}` → "
            f"**diagnosis:** {str(h.get('diagnosis') or '')[:200]} → "
            f"**action:** {h.get('action')}"
            for i, h in enumerate(history)
        )
        runbook = (
            f"## Runbook — stage `{spec.name}` could not self-heal\n\n"
            f"**Run:** `{task.id}` · **Agent:** {spec.resolved_agent() or 'n/a'} · "
            f"**Recovery rounds used:** {len(history)}\n\n"
            f"**Final error:**\n```\n{error[:700]}\n```\n\n"
            f"### What the recovery agent tried\n{chrono}\n\n"
            f"{await self._runbook_advice(spec, error, history, task)}"
        )
        task.agent_context[f"runbook:{spec.name}"] = runbook[:6000]

        rec = task.agent_context.get(f"heal:{spec.name}") or {}
        bug = rec.get("bug_issue")
        scm = self._deps.scm
        if scm is not None and bug and not getattr(task, "dry_run", False):
            try:
                from devai.services.prompt_guard import neutralize_for_issue

                await scm.add_comment(task.repo, bug, neutralize_for_issue(runbook))
                await scm.add_labels(task.repo, bug, ["devai:needs-human", "devai:runbook"])
            except Exception:  # noqa: BLE001
                logger.exception("runbook post failed for stage %s", spec.name)
        self._emit(
            task,
            StageEvent(
                f"heal:{spec.name}",
                StageEventPhase.FAILED,
                stage_type="recovery",
                agent=self._HEAL_AGENT,
                lane=spec.lane,
                error=(
                    f"recovery exhausted after {len(history)} round(s) — runbook posted"
                    + (f" to bug #{bug}" if bug else "")
                ),
            ),
        )

    async def _runbook_advice(
        self, spec: StageSpec, error: str, history: list[dict[str, Any]], task: DevAITask | None = None
    ) -> str:
        """LLM-drafted 'what a human should check' section; mechanical
        fallback when no LLM is usable."""
        llm = await self._deps.role_llm_for_principal(getattr(task, "triggered_by", "") or "", "utility")
        if llm is not None and getattr(llm, "provider_name", "noop") != "noop":
            try:
                from devai.adapters.llm.base import LLMMessage, LLMRequest, LLMRole
                from devai.services.prompt_guard import wrap_untrusted

                tried = "; ".join(str(h.get("diagnosis") or "")[:150] for h in history[-4:])
                prompt = (
                    f"An autonomous pipeline stage {spec.name!r} failed repeatedly and automated "
                    f"recovery gave up.\n{wrap_untrusted(error, 'final error', limit=1000)}\n\n"
                    f"Diagnoses already tried "
                    f"(none worked): {tried}\n\nWrite EXACTLY two markdown sections:\n"
                    "### Likely root cause\n<2-3 sentences>\n"
                    "### What a human should check\n<numbered list of up to 5 concrete steps>"
                )
                response = await llm.generate(
                    LLMRequest(
                        messages=[LLMMessage(role=LLMRole.USER, content=prompt)],
                        max_tokens=500,
                        temperature=0.0,
                        model=str(getattr(self._deps.config, "llm_model_utility", "") or ""),
                        extra={
                            "agent": self._HEAL_AGENT,
                            "triggered_by": getattr(task, "triggered_by", "") or "",
                            "run_id": getattr(task, "id", "") or "",
                        },
                    )
                )
                text = (response.text or "").strip()
                if text:
                    return text
            except Exception:  # noqa: BLE001
                logger.exception("runbook advice generation failed")
        return (
            "### What a human should check\n"
            "1. Read the final error above — it survived every automated diagnosis.\n"
            "2. Check the stage's agent logs (Logs tab, errors filter) for the failing tool calls.\n"
            "3. Re-run the failing operation manually with the same inputs.\n"
            "4. Resume the run from the failed stage once fixed."
        )

    async def _diagnose_failure(self, spec: StageSpec, task: DevAITask, error: str) -> dict[str, Any] | None:
        """LLM root-cause + recovery plan. None when no usable LLM or the
        response is unusable — recovery degrades to plain on_failure."""
        llm = await self._deps.role_llm_for_principal(task.triggered_by or "", "utility")
        if llm is None or getattr(llm, "provider_name", "noop") == "noop":
            return None
        try:
            from devai.adapters.llm.base import LLMMessage, LLMRequest, LLMRole

            prior = [
                h
                for h in (task.agent_context.get("heal_history") or [])
                if isinstance(h, dict) and h.get("stage") == spec.name
            ]
            prior_txt = (
                "\nPrior recovery attempts on this stage (they did NOT fix it — propose something DIFFERENT):\n"
                + "\n".join(f"- {h.get('diagnosis', '')[:200]}" for h in prior[-3:])
                if prior
                else ""
            )
            completed = ", ".join(task.stages_completed[-12:]) or "none"
            # The error text is attacker-influenceable (CI logs, tool output,
            # repo content) — fence it so the diagnosis model reads it as
            # data, not as instructions to launder into guidance.
            from devai.services.prompt_guard import wrap_untrusted

            prompt = (
                "You are the recovery specialist of an autonomous software-delivery pipeline. "
                f"Stage {spec.name!r} (agent: {spec.resolved_agent() or 'n/a'}, handler: {spec.stage}) "
                "failed after all retries.\n\n"
                f"{wrap_untrusted(error, 'stage error / failure logs', limit=1500)}\n\n"
                f"Run intent: {(task.intent or '')[:400]}\n"
                f"Repo: {task.repo}\nStages completed so far: {completed}{prior_txt}\n\n"
                "Decide how to recover. Reply with STRICT JSON only (no markdown fences):\n"
                '{"diagnosis": "<2-3 sentence root cause>",\n'
                ' "action": "retry" | "ask_user" | "abort",\n'
                ' "guidance": "<concrete corrective instructions injected into the stage\'s next attempt>",\n'
                ' "user_message": "<only for ask_user: the decision you need from the human, with details>"}\n\n'
                'Prefer "retry" with concrete guidance whenever the failure is autonomously fixable '
                "(malformed output, missing field, contract violation, transient API error, bad assumption). "
                'Use "ask_user" ONLY when a human decision is genuinely required (credentials, destructive '
                'action, conflicting requirements). Use "abort" only when re-running cannot possibly help.'
            )
            response = await llm.generate(
                LLMRequest(
                    messages=[LLMMessage(role=LLMRole.USER, content=prompt)],
                    max_tokens=700,
                    temperature=0.0,
                    model=str(getattr(self._deps.config, "llm_model_utility", "") or ""),
                    extra={
                        "agent": self._HEAL_AGENT,
                        "triggered_by": task.triggered_by or "",
                        "run_id": task.id,
                    },
                )
            )
            data = _parse_json_lenient(response.text or "")
            if not isinstance(data, dict) or data.get("action") not in ("retry", "ask_user", "abort"):
                logger.warning("recovery diagnosis for %s unusable: %r", spec.name, str(data)[:200])
                return None
            return data
        except Exception:  # noqa: BLE001 — recovery must never crash the run
            logger.exception("recovery diagnosis failed for stage %s", spec.name)
            return None

    async def _await_heal_approval(self, spec: StageSpec, task: DevAITask, decision: dict[str, Any], _hev: Any) -> bool:
        """Raise a dynamic gate with the recovery proposal and wait for the
        human decision (same key the dashboard Approve/Reject writes)."""
        sm = self._deps.state_manager
        redis = getattr(sm, "redis", None) if sm is not None else None
        if redis is None:
            return True  # no decision surface (tests) — degrade open
        gate_name = f"heal-{spec.name}"
        gates = task.agent_context.setdefault("dynamic_gates", [])
        if isinstance(gates, list) and not any(isinstance(g, dict) and g.get("gate") == gate_name for g in gates):
            gates.append(
                {
                    "gate": gate_name,
                    "title": f"Recovery plan: {spec.name}",
                    "kind": "heal_approval",
                    "stage": spec.name,
                    "agent": self._HEAL_AGENT,
                    "intent": (task.intent or "")[:300],
                    "error": str(
                        decision.get("error") or task.agent_context.get(f"heal:{spec.name}", {}).get("error") or ""
                    )[:600],
                    "diagnosis": str(decision.get("diagnosis") or "")[:800],
                    "plan_summary": str(decision.get("guidance") or "")[:800],
                    "questions": [q for q in [str(decision.get("user_message") or "").strip()] if q],
                    "requested_at": time.time(),
                }
            )
        key = f"devai:pipeline:gate:{task.id}:{gate_name}"
        prior_state = task.state
        task.transition(TaskState.AWAITING_APPROVAL)
        self._emit(
            task,
            _hev(
                StageEventPhase.STARTED,
                message=f"recovery needs your approval: {str(decision.get('user_message') or '')[:200]}",
            ),
        )
        waited = 0.0
        while waited < self._GATE_MAX_WAIT_SECONDS:
            if not await self._check_run_control(task):
                return False  # stopped by user while waiting
            try:
                verdict = await redis.get(key)
            except Exception:  # noqa: BLE001 — a Redis blip must not kill the wait
                verdict = None
            if verdict is not None:
                approved = str(verdict).lower() == "approved"
                if approved:
                    task.transition(
                        prior_state if prior_state not in (TaskState.PENDING, TaskState.QUEUED) else TaskState.RUNNING
                    )
                return approved
            await asyncio.sleep(self._GATE_POLL_SECONDS)
            waited += self._GATE_POLL_SECONDS
        return False

    _GATE_POLL_SECONDS = 3.0
    _GATE_MAX_WAIT_SECONDS = 24 * 3600.0

    async def _resolve_gate(self, spec: StageSpec, task: DevAITask, _ev: Any) -> bool:
        """Resolve a human-approval gate before the stage runs.

        Returns True to proceed, False when the run stops here (rejected,
        timed out, or stopped by run-control). Decision key:
        ``devai:pipeline:gate:{task_id}:{stage}`` — the same key the
        dashboard's Approve/Reject endpoints write.

        Autonomy (per-run ``agent_context['autonomy']``, falling back to
        ``DEVAI_PIPELINE_DEFAULT_AUTONOMY``):
          full        — self-approve with an audit decision; never prompt.
          auto/gated  — a HARD gate is a hard rule: pause in
                        AWAITING_APPROVAL (banner + notification surface),
                        wait up to the gate timeout (default 30m), then
                        time out RESUMABLY — the run lands in stage_failed
                        and Continue re-requests approval at this gate.
        No Redis (tests/minimal deps) → proceed.
        """
        sm = self._deps.state_manager
        redis = getattr(sm, "redis", None) if sm is not None else None
        if redis is None:
            return True
        key = f"devai:pipeline:gate:{task.id}:{spec.name}"

        try:
            decision = await redis.get(key)
        except Exception:  # noqa: BLE001 — a Redis blip must not kill the run
            logger.exception("gate %s: decision read failed — proceeding", spec.name)
            return True
        autonomy = str(task.agent_context.get("autonomy") or self._default_autonomy).lower()

        if decision is None and autonomy == "full":
            # ONLY the explicitly chosen "fully autonomous" mode self-approves.
            # Smart (auto) used to as well — but a hard gate the user can see
            # on the DAG that silently approves itself reads as a broken
            # promise; auto now waits like gated does.
            try:
                await redis.set(key, "approved", ex=86400)
                await redis.set(f"{key}:approver", "autonomy:full", ex=86400)
            except Exception:  # noqa: BLE001
                logger.exception("gate %s: auto-approve write failed — proceeding", spec.name)
            self._emit(task, _ev(StageEventPhase.STARTED, message="gate auto-approved (autonomy=full)"))
            return True

        if decision is None:
            # Pause for the human. The state flip is what makes the dashboard
            # banner appear (list_gates: reached + undecided = pending).
            prior_state = task.state
            task.current_stage = spec.name
            task.transition(TaskState.AWAITING_APPROVAL)
            gate_timeout = float(getattr(self._deps.config, "pipeline_gate_timeout_seconds", 1800) or 1800)
            self._emit(
                task,
                _ev(
                    StageEventPhase.STARTED,
                    message=f"waiting for your approval (times out in {int(gate_timeout / 60)}m — resumable)",
                ),
            )
            waited = 0.0
            while decision is None and waited < gate_timeout:
                if not await self._check_run_control(task):
                    return False  # stopped by user while waiting
                await asyncio.sleep(self._GATE_POLL_SECONDS)
                waited += self._GATE_POLL_SECONDS
                try:
                    decision = await redis.get(key)
                except Exception:  # noqa: BLE001
                    logger.exception("gate %s: decision poll failed", spec.name)
            if decision is None:
                # RESUMABLE timeout — stage_failed (not cancelled) so the
                # Continue button re-enqueues the run; the gate stage never
                # completed, so resume re-reaches it and asks again fresh.
                task.error = (
                    f"approval gate {spec.name!r} timed out after {int(gate_timeout / 60)}m — "
                    "press Continue to resume and re-request approval"
                )
                task.failed_stage = spec.name
                task.stages_failed.append(spec.name)
                task.current_stage = ""
                task.transition(TaskState.STAGE_FAILED)
                self._emit(task, _ev(StageEventPhase.FAILED, error=task.error))
                return False
            task.transition(
                prior_state if prior_state not in (TaskState.PENDING, TaskState.QUEUED) else TaskState.RUNNING
            )

        if str(decision).lower() == "rejected":
            task.error = f"rejected at gate {spec.name!r}"
            task.failed_stage = spec.name
            task.transition(TaskState.CANCELLED)
            self._emit(task, _ev(StageEventPhase.FAILED, error=task.error))
            return False
        return True

    async def _handle_failure(
        self,
        spec: StageSpec,
        stage: PipelineStage,
        task: DevAITask,
        error: str,
        failure_state: TaskState,
        duration_ms: float,
    ) -> None:
        task.stages_failed.append(spec.name)
        self._emit(
            task,
            StageEvent(
                spec.name,
                StageEventPhase.FAILED,
                duration_ms=duration_ms,
                error=error,
                stage_type=spec.type,
                gate=spec.gate,
                agent=spec.resolved_agent(),
                lane=spec.lane,
            ),
        )

        if spec.on_failure == "rollback":
            try:
                await stage.rollback(task)
            except Exception:  # noqa: BLE001 — rollback failure shouldn't mask the original
                logger.exception("rollback failed for stage %s", spec.name)

        if spec.on_failure in {"stop", "rollback"}:
            task.error = error
            task.failed_stage = spec.name
            task.transition(failure_state)
        # on_failure=continue: leave state alone, executor proceeds.
        task.current_stage = ""

    def _emit(self, task: DevAITask, event: StageEvent) -> None:
        task.record_event(event)
        if self._event_cb is not None:
            try:
                self._event_cb(task, event)
            except Exception:  # noqa: BLE001
                logger.exception("event callback failed for %s", event.stage)


# ──────────────────────────────────────────────────────────────────────────
# Topo sort: stages → levels
# ──────────────────────────────────────────────────────────────────────────


def _topo_sort(stages: Iterable[StageSpec]) -> list[list[StageSpec]]:
    """Kahn's algorithm → levels of stages with no inter-level dependencies.

    Delegates to :func:`devai.blueprint.planner.topological_levels` so the
    in-process executor and the Temporal workflow share one ordering routine.
    Re-raises cycle errors as :class:`BlueprintExecutionError`.
    """
    try:
        return topological_levels(list(stages))
    except ValueError as e:
        raise BlueprintExecutionError(str(e)) from e


__all__ = ["BlueprintExecutor", "BlueprintExecutionError"]
