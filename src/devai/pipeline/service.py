"""PipelineService — FastAPI-side wrapper around `Pipeline`.

`Pipeline` itself is a queue + executor. PipelineService is what the rest
of the codebase actually talks to:

- Start/stop with FastAPI's lifespan.
- Hand out a dispatch surface (`dispatch(intent=..., blueprint=...)`).
- Maintain a bounded ring buffer of stage events for SSE replay.
- Mirror state mutations into StateManager.persist_task for durability.
- Expose helpers (list_blueprints, list_stages, list_tasks, get_task).

Stored on `app.state.pipeline_service`. Webhook routes, the SRE server,
the chat agent, and the dashboard all read from the same instance.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from devai.pipeline.types import (
    FAILURE_STATES,
    TERMINAL_STATES,
    DevAITask,
    StageEvent,
    StageEventPhase,
    TaskState,
)

if TYPE_CHECKING:
    from devai.adapters.event_bus.base import EventBusAdapter
    from devai.config import Settings
    from devai.core.event_bus import EventBus
    from devai.core.state import StateManager
    from devai.pipeline.pipeline import Pipeline
    from devai.scm.base import SCMClient

logger = logging.getLogger(__name__)

# Stage-event phases that represent a terminal transition — the ones worth one
# telemetry metric each (STARTED is omitted so we count each stage once).
_TELEMETRY_STAGE_PHASES = frozenset({"completed", "failed", "skipped"})


def _provider_for_model(model: str) -> str:
    """Provider family from a model id — for ledger rows built from turn
    envelopes, where the provider isn't otherwise visible."""
    m = (model or "").lower()
    if m.startswith("claude"):
        return "anthropic"
    if m.startswith("gemini"):
        return "vertex_gemini"
    if m.startswith(("llama", "mixtral")):
        return "groq"
    if m.startswith(("gpt", "o1", "o3", "o4")):
        return "openai"
    return "unknown"


class PipelineService:
    """Operational facade over `Pipeline`.

    Lifecycle:
        svc = PipelineService(config, scm, state_manager, event_bus)
        await svc.start()
        ...
        task_id = await svc.dispatch(intent="...", blueprint="alm-pipeline", repo="org/repo")
        ...
        await svc.stop()
    """

    def __init__(
        self,
        config: Settings,
        *,
        scm: SCMClient | None = None,
        state_manager: StateManager | None = None,
        event_bus: EventBus | None = None,
        event_bus_adapter: EventBusAdapter | None = None,
        blueprint_dir: str | Path | None = None,
        registry_client: Any = None,
        settings_service: Any = None,
        telemetry: Any = None,
    ) -> None:
        self.config = config
        self.scm = scm
        self.state_manager = state_manager
        self.event_bus = event_bus
        self.event_bus_adapter = event_bus_adapter
        # TelemetryAdapter (optional) — emits one OTel metric per stage event.
        # None / noop is normal (telemetry off); _on_event tolerates it.
        self.telemetry = telemetry
        # aregistry client — handed to JobRunnerStage via StageDeps.extra
        # so dispatch can resolve agent profiles before submitting Jobs.
        # None is normal (DEVAI_REGISTRY_URL unset) — stages tolerate it.
        self.registry_client = registry_client
        # SettingsService (optional) — threaded into StageDeps so stages can
        # resolve a per-user settings overlay from the run's principal.
        self.settings_service = settings_service
        self.blueprint_dir = Path(blueprint_dir or config.pipeline_blueprint_dir)

        self._pipeline: Pipeline | None = None
        self._started = False
        self._owns_event_bus_adapter = False  # True when we constructed it
        self._ring: deque[tuple[float, str, dict[str, Any]]] = deque(
            maxlen=getattr(config, "pipeline_event_ring_size", 1000)
        )
        self._sse_queues: list[asyncio.Queue[tuple[float, str, dict[str, Any]]]] = []
        self._lock = asyncio.Lock()
        # Strong references to fire-and-forget publish tasks so the event loop
        # can't GC them mid-flight (asyncio only holds a weak ref). Cleared by
        # a done-callback when each task finishes.
        self._publish_tasks: set[asyncio.Task[None]] = set()
        # Per-blueprint stage counts, cached for progress derivation.
        self._bp_stage_counts: dict[str, int] = {}

        # Turn-level agent observability: LLM providers emit per-turn
        # envelopes (usage, narration, tool calls) through the process-
        # global sink — register ours so they land on this run-event spine.
        try:
            from devai.services.agent_turns import set_turn_sink

            set_turn_sink(self._on_agent_turn)
        except Exception:  # noqa: BLE001 — observability must not break startup
            logger.exception("turn sink registration failed")

    async def _on_agent_turn(self, run_id: str, envelope: dict[str, Any]) -> None:
        """Sink for turn-level agent envelopes (devai.services.agent_turns).

        Same fan-out as stage events minus the heavyweight sinks: ring (SSE
        replay) + live SSE queues + durable per-run Redis log. Turns are an
        order of magnitude chattier than stages, so no NATS mirror, no
        telemetry metric, no task mutation.
        """
        ts = float(envelope.get("timestamp") or time.time())
        payload = {**envelope, "task_id": run_id}
        self._ring.append((ts, run_id, payload))
        for q in list(self._sse_queues):
            try:
                q.put_nowait((ts, run_id, payload))
            except asyncio.QueueFull:
                logger.debug("SSE queue full — dropping agent_turn")
        if self.state_manager is not None:
            try:
                self._persist_run_log(run_id, [payload], ts)
            except RuntimeError:
                pass  # no running loop (test context)
        if envelope.get("kind") == "turn":
            self._account_turn_usage(run_id, envelope)

    def _account_turn_usage(self, run_id: str, envelope: dict[str, Any]) -> None:
        """Billing for the legacy provider loops (ClaudeProvider et al).

        Adapter-path calls are accounted by InstrumentedLLMAdapter; the
        legacy agents talk to the SDK directly and their only externally
        visible usage signal is the per-turn envelope. Record it into the
        usage ledger (per-user/per-model analytics) and, when the run is in
        trial mode (no user connector), draw down that user's trial budget.
        Best-effort and fire-and-forget — billing must never break the loop.
        """
        try:
            tok_in = int(envelope.get("usage_in") or 0)
            tok_out = int(envelope.get("usage_out") or 0)
            if tok_in <= 0 and tok_out <= 0:
                return
            email = str(envelope.get("triggered_by") or "")
            model = str(envelope.get("model") or "")
            provider = _provider_for_model(model)
            cost = 0.0
            try:
                from devai.analytics.pricing import estimate_cost

                cost = estimate_cost(provider, model, tok_in, tok_out)
            except Exception:  # noqa: BLE001
                pass

            from devai.analytics.usage_ledger import get_global_ledger

            ledger = get_global_ledger()
            if ledger is not None:
                import datetime

                self._spawn(
                    ledger.record(
                        day=datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d"),
                        provider=provider,
                        model=model,
                        tokens_in=tok_in,
                        tokens_out=tok_out,
                        cost_usd=cost,
                        duration_ms=0.0,
                        triggered_by=email,
                        agent=str(envelope.get("agent") or ""),
                        run_id=run_id,
                    )
                )
            # Durable sink: a completed agent_executions row so the Postgres
            # analytics rollups see legacy-loop calls too.
            self._spawn(
                self._persist_turn_execution(
                    run_id=run_id,
                    agent=str(envelope.get("agent") or ""),
                    provider=provider,
                    model=model,
                    tok_in=tok_in,
                    tok_out=tok_out,
                    cost=cost,
                )
            )
            if envelope.get("trial") and email:
                from devai.settings.trial import get_trial_meter

                meter = get_trial_meter(self.config)
                self._spawn(meter.add(email, tok_in + tok_out))
        except RuntimeError:
            pass  # no running loop (sync test context)
        except Exception:  # noqa: BLE001
            logger.debug("turn usage accounting failed", exc_info=True)

    @staticmethod
    async def _persist_turn_execution(
        *, run_id: str, agent: str, provider: str, model: str, tok_in: int, tok_out: int, cost: float
    ) -> None:
        try:
            from devai.services.database import get_global_db

            db = await get_global_db()
            if db is None:
                return
            await db.record_llm_call(
                run_id=run_id,
                agent_name=agent or provider,
                provider=provider,
                model=model,
                tokens_input=tok_in,
                tokens_output=tok_out,
                cost_usd=cost,
                duration_ms=0.0,
            )
        except Exception:  # noqa: BLE001 — analytics persistence is best-effort
            logger.debug("turn execution persistence failed", exc_info=True)

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def start(self) -> None:
        """Build the Pipeline, load blueprints, start workers.

        Safe to call twice — second call is a no-op.
        """
        if self._started:
            return

        from devai.adapters.event_bus import create_event_bus_adapter
        from devai.pipeline.bootstrap import build_runtime
        from devai.pipeline.pipeline import Pipeline  # local import to avoid cycle

        # Make the StateManager / Database resolvable by the memory factory
        # without them needing their own env vars. The factory looks at
        # `settings.state_manager` and `settings.database` first.
        try:
            if self.state_manager is not None and not hasattr(self.config, "state_manager"):
                object.__setattr__(self.config, "state_manager", self.state_manager)  # type: ignore[arg-type]
        except (TypeError, AttributeError):
            # Pydantic Settings is frozen by default — fall through; the
            # factory will use `settings.redis_url` instead.
            pass

        # Event-bus adapter — built here and connected immediately so
        # `_on_event` can publish stage events to NATS without needing
        # to lazy-init on the hot path. If the caller already injected
        # an adapter (test path), we use that one and don't take
        # ownership of close().
        if self.event_bus_adapter is None:
            self.event_bus_adapter = create_event_bus_adapter(self.config)
            self._owns_event_bus_adapter = True
            try:
                await self.event_bus_adapter.connect()
                logger.info(
                    "PipelineService event-bus connected: provider=%s",
                    self.event_bus_adapter.provider_name,
                )
            except Exception:
                # Factory's create_and_connect is what we'd normally call,
                # but we wanted ownership tracking. Degrade to noop on
                # connect failure rather than crashing the pipeline.
                logger.exception(
                    "PipelineService event-bus connect failed (%s) — degrading to in-process noop",
                    self.event_bus_adapter.provider_name,
                )
                from devai.adapters.event_bus.noop import NoopEventBusAdapter

                self.event_bus_adapter = NoopEventBusAdapter()
                await self.event_bus_adapter.connect()

        # Build StageDeps + StageRegistry via the shared bootstrap so the
        # in-process pipeline and the Temporal worker wire every adapter,
        # the specialization catalog, seed crews, capability adapters and the
        # K8s runtime identically. (Event-bus adapter is owned here and passed
        # in already-connected.)
        bundle = await build_runtime(
            self.config,
            scm=self.scm,
            state_manager=self.state_manager,
            event_bus=self.event_bus,
            event_bus_adapter=self.event_bus_adapter,
            registry_client=getattr(self, "registry_client", None),
            settings_service=getattr(self, "settings_service", None),
        )
        self._runtime_bundle = bundle
        self._memory_adapter = bundle.memory_adapter
        self._llm_adapter = bundle.llm_adapter
        self._k8s_runtime = bundle.k8s_runtime
        self._job_watcher = bundle.job_watcher
        deps = bundle.deps
        self._pipeline = Pipeline(
            deps,
            blueprint_dir=self.blueprint_dir,
            concurrency=getattr(self.config, "pipeline_concurrency", 4),
            default_stage_timeout=float(getattr(self.config, "pipeline_default_stage_timeout", 900)),
        )
        self._pipeline.add_event_callback(self._on_event)

        try:
            self._pipeline.load_blueprints()
        except Exception as e:  # noqa: BLE001 — surface but don't block startup
            logger.error("pipeline blueprint load failed: %s", e)

        await self._pipeline.start()
        self._started = True
        logger.info(
            "PipelineService started — %d blueprints, %d stages",
            len(self._pipeline.list_blueprints()),
            len(self._pipeline._registry.known_stages()),  # noqa: SLF001
        )

        # Resume-on-boot: re-enqueue runs that were orphaned by a previous pod
        # roll (queued/running in Redis but in no live pod's queue — including
        # any stranded BEFORE the durable queue existed). enqueue_task is
        # idempotent, so all replicas booting at once converge to one enqueue.
        await self._reconcile_orphans()

        # Orphan reconciliation must be CONTINUOUS, not boot-only: a dispatch
        # can persist the snapshot and then fail the enqueue (Redis blip), or
        # an ack path can drop a run — leaving it 'pending' forever with an
        # empty queue. Re-running the reconciler periodically re-enqueues any
        # non-terminal, non-active run; idempotent via the SADD guard, and
        # the claim/stop guards make resurrection of finished runs impossible.
        reconcile_every = max(5, int(getattr(self.config, "pipeline_reconcile_interval", 15) or 15))

        async def _reconcile_loop() -> None:
            while True:
                await asyncio.sleep(reconcile_every)
                try:
                    await self._reconcile_orphans()
                except Exception:  # noqa: BLE001
                    logger.exception("periodic reconcile failed (non-fatal)")

        self._spawn(_reconcile_loop())

    async def _reconcile_orphans(self) -> None:
        """Find non-terminal persisted runs that no worker owns and re-enqueue
        them onto the durable queue. Safe no-op outside durable mode.

        Resumable = not terminal, not failed, not waiting on a human gate, and
        not already active (queued/processing). The executor skips completed
        stages on the re-run, so a mid-flight orphan resumes from where it
        stopped instead of re-doing finished work.
        """
        pipe = self._pipeline
        sm = self.state_manager
        if pipe is None or sm is None or not getattr(pipe, "_durable", False):
            return
        try:
            tasks = await self.list_persisted_tasks(limit=500)
        except Exception:  # noqa: BLE001
            logger.exception("reconcile: failed to list persisted tasks")
            return

        # AWAITING_APPROVAL is NOT skipped: a run only legitimately awaits
        # while a live worker holds the gate poll — after a pod roll that
        # await is gone and the run is orphaned forever (live incident: the
        # user approved, the gate key said 'approved', and nothing ever
        # consumed it). The is_task_active check below protects runs whose
        # worker is genuinely alive; orphans re-enqueue, the gate stage
        # re-runs, reads the recorded decision, and proceeds (or re-pauses
        # harmlessly when still undecided).
        skip_states = TERMINAL_STATES | FAILURE_STATES
        resumed = 0
        for td in tasks:
            try:
                state = TaskState(td.get("state", "pending"))
            except ValueError:
                state = TaskState.PENDING
            if state in skip_states:
                continue
            tid = td["id"]
            if await sm.is_task_active(tid):
                # Active-set membership alone is NOT liveness: the set has no
                # TTL, so a hard-killed worker (pod roll during a gate wait —
                # live incident) leaves a stale member behind and the run
                # looks active forever. The claim key IS liveness.
                if await sm.is_task_live(tid):
                    continue  # genuinely executing on a live worker
                # Already queued (graceful hand-off / reaper requeue) → a
                # worker will claim it; clearing the member here would let a
                # second enqueue through the dedup and double-execute.
                try:
                    if await sm.redis.lpos(sm.PIPELINE_QUEUE_KEY, tid) is not None:
                        continue
                except Exception:  # noqa: BLE001
                    logger.debug("reconcile: queue check failed for %s", tid, exc_info=True)
                    continue
                logger.warning(
                    "reconcile: %s is in the active set but its claim expired — clearing stale marker and resuming",
                    tid,
                )
                if not await sm.clear_stale_active(tid):
                    continue  # a live worker re-claimed in the race window
            if await pipe.resubmit_persisted(td):
                resumed += 1
        if resumed:
            logger.warning("reconcile: resumed %d orphaned run(s) on boot", resumed)
        else:
            logger.info("reconcile: no orphaned runs to resume")

    async def stop(self) -> None:
        if not self._started or self._pipeline is None:
            return
        await self._pipeline.stop()
        # Memory adapters that own connections (mem0 HTTP client, zep HTTP
        # client) clean up here. Adapters that share a connection with the
        # host (Redis, pgvector) implement close() as a no-op.
        memory = getattr(self, "_memory_adapter", None)
        if memory is not None:
            try:
                await memory.close()
            except Exception:  # noqa: BLE001
                logger.exception("memory adapter close failed")
        llm = getattr(self, "_llm_adapter", None)
        if llm is not None:
            try:
                await llm.close()
            except Exception:  # noqa: BLE001
                logger.exception("llm adapter close failed")
        if self._owns_event_bus_adapter and self.event_bus_adapter is not None:
            try:
                await self.event_bus_adapter.close()
            except Exception:  # noqa: BLE001
                logger.exception("event_bus adapter close failed")
        watcher = getattr(self, "_job_watcher", None)
        if watcher is not None:
            try:
                await watcher.stop()
            except Exception:  # noqa: BLE001
                logger.exception("JobWatcher stop failed")
        runtime = getattr(self, "_k8s_runtime", None)
        if runtime is not None:
            try:
                await runtime.close()
            except Exception:  # noqa: BLE001
                logger.exception("K8sJobRuntime close failed")
        self._started = False
        logger.info("PipelineService stopped")

    # ── Dispatch surface ─────────────────────────────────────────────

    async def dispatch(
        self,
        *,
        intent: str,
        blueprint: str | None = None,
        repo: str = "",
        trigger_type: str = "manual",
        label: str = "",
        agent_context: dict[str, Any] | None = None,
        principal: dict[str, Any] | None = None,
        trace_id: str | None = None,
        team_id: str = "",
        crew_id: str = "",
        autonomy: str = "",
    ) -> str:
        """Enqueue a new task. Returns its id.

        Returns the task id even if the blueprint is unknown — but in that
        case raises PipelineError. Callers should treat this as create+enqueue.

        ``principal`` / ``trace_id`` are stamped onto the DevAITask so the
        executor, every stage, and the persisted record all know which
        human triggered the work. Both are optional — passing None is the
        same as system-triggered (e.g. SRE cron). ``team_id`` / ``crew_id``
        scope the run to a human team and an AI crew (both optional —
        empty means unscoped, the pre-teams behavior).
        """
        self._ensure_started()
        assert self._pipeline is not None  # for type-checker

        bp = blueprint or self.config.pipeline_default_blueprint
        task = DevAITask(
            intent=intent,
            blueprint=bp,
            repo=repo,
            trigger_type=trigger_type,
            label=label,
            principal=dict(principal) if principal else None,
            trace_id=trace_id,
            triggered_by=(principal.get("email") if principal else None),
            team_id=team_id,
            crew_id=crew_id,
        )
        if agent_context:
            task.agent_context.update(agent_context)
        # Also stash identity into agent_context so stages that don't
        # know about the new task fields can still read it from the
        # handover bag (which is the documented stage contract).
        if principal:
            task.agent_context.setdefault("principal", dict(principal))
        if trace_id:
            task.agent_context.setdefault("trace_id", trace_id)
        # Per-run gate behavior: "full" (self-approving gates, end-to-end) or
        # "gated" (pause at every gate). Empty → executor falls back to
        # DEVAI_PIPELINE_DEFAULT_AUTONOMY.
        if autonomy in ("full", "gated"):
            task.agent_context["autonomy"] = autonomy
        await self._pipeline.submit(task)
        await self._publish_task_event("created", task)
        return task.id

    async def run_once(
        self,
        *,
        intent: str,
        blueprint: str | None = None,
        repo: str = "",
        trigger_type: str = "manual",
        agent_context: dict[str, Any] | None = None,
        principal: dict[str, Any] | None = None,
        trace_id: str | None = None,
        dry_run: bool = False,
    ) -> DevAITask:
        """Synchronous dispatch — submit, wait, return the final task.

        Used by the SRE scanner where we want to await completion inline.
        When ``dry_run`` is true the run executes for real but every
        side-effecting stage suppresses its writes (see DevAITask.dry_run).
        """
        self._ensure_started()
        assert self._pipeline is not None

        bp = blueprint or self.config.pipeline_default_blueprint
        task = DevAITask(
            intent=intent,
            blueprint=bp,
            repo=repo,
            trigger_type=trigger_type,
            principal=dict(principal) if principal else None,
            trace_id=trace_id,
            triggered_by=(principal.get("email") if principal else None),
            dry_run=dry_run,
        )
        if agent_context:
            task.agent_context.update(agent_context)
        if principal:
            task.agent_context.setdefault("principal", dict(principal))
        if trace_id:
            task.agent_context.setdefault("trace_id", trace_id)
        await self._publish_task_event("created", task)
        result = await self._pipeline.run_once(task)
        # run_once is synchronous from the caller's POV, so emit a terminal
        # event here too — the stage-event path also fires, but a
        # subscriber that's listening for task.* without subject wildcards
        # gets a definitive end-of-run signal.
        terminal_kind = (
            "failed" if result.state in FAILURE_STATES else "completed" if result.state == TaskState.COMPLETED else None
        )
        if terminal_kind:
            await self._publish_task_event(terminal_kind, result)
        return result

    # ── Read surface ─────────────────────────────────────────────────

    @property
    def k8s_runtime(self) -> Any:
        """The shared K8sJobRuntime (or None if the runtime didn't start).

        Lets on-demand consumers like the PreviewService reuse the same
        connected client + RuntimeConfig instead of building their own.
        """
        return getattr(self, "_k8s_runtime", None)

    def register_blueprint(self, bp: Any) -> bool:
        """Hot-register an already-loaded Blueprint so it's immediately
        runnable. Used by the authoring service when a user publishes a
        blueprint built in the visual builder. Returns False (never raises)
        if the runtime isn't up or a stage is unknown."""
        if self._pipeline is None:
            return False
        try:
            self._pipeline.register_blueprint(bp)
            return True
        except Exception:  # noqa: BLE001
            logger.warning("register_blueprint(%s) failed", getattr(bp, "name", "?"), exc_info=True)
            return False

    def list_blueprints(self) -> list[dict[str, Any]]:
        if self._pipeline is None:
            return []
        out = []
        for name in self._pipeline.list_blueprints():
            bp = self._pipeline.get_blueprint(name)
            out.append(
                {
                    "name": bp.name,
                    "description": bp.description,
                    "stage_count": len(bp.stages),
                    "metadata": dict(bp.metadata),
                }
            )
        return out

    def get_blueprint_graph(self, name: str) -> dict[str, Any] | None:
        """Return a render-ready graph (`{nodes, edges, lanes, ...}`) for one
        blueprint, or None if it isn't loaded.

        This is what the dashboards fetch to draw the pipeline flow without
        hardcoding nodes — the blueprint YAML becomes the source of truth.
        """
        if self._pipeline is None:
            return None
        try:
            bp = self._pipeline.get_blueprint(name)
        except Exception:  # noqa: BLE001 — unknown blueprint → 404 at the route
            return None
        from devai.blueprint.graph import build_blueprint_graph

        return build_blueprint_graph(bp)

    def list_stage_keys(self) -> list[str]:
        if self._pipeline is None:
            return []
        return self._pipeline._registry.known_stages()  # noqa: SLF001

    def list_tasks_in_memory(self) -> list[dict[str, Any]]:
        """In-memory snapshot — tasks currently tracked by the running
        Pipeline. For historical tasks across restarts, use
        StateManager.list_pipeline_tasks() (Redis-backed)."""
        if self._pipeline is None:
            return []
        return [t.to_dict() for t in self._pipeline.list_tasks()]

    def get_task_in_memory(self, task_id: str) -> dict[str, Any] | None:
        if self._pipeline is None:
            return None
        task = self._pipeline.get_task(task_id)
        return task.to_dict() if task is not None else None

    async def request_rollback(self, task_id: str, sha: str) -> bool:
        """Record a checkpoint-rollback request and publish it.

        The actual `git reset --hard` runs in the task's runner pod (via the
        `rollback` tool); this records the user's intent on the task and emits
        an event the runner / dashboard can react to. Returns False when the
        task isn't in memory.
        """
        if self._pipeline is None:
            return False
        task = self._pipeline.get_task(task_id)
        if task is None:
            return False
        task.agent_context.setdefault("rollback_requests", []).append(sha)
        if self.event_bus_adapter is not None:
            try:
                await self.event_bus_adapter.publish(
                    f"devai.pipeline.task.{task_id}.rollback", {"task_id": task_id, "sha": sha}
                )
            except Exception:  # noqa: BLE001
                logger.debug("rollback publish failed", exc_info=True)
        return True

    async def inject_message(self, task_id: str, message: str) -> bool:
        """Record a user message onto a running task's handover bag.

        Appended to ``agent_context["injections"]`` (and surfaced as an event) so
        a re-planning stage — the Supervisor or a crew lead — can pick it up.
        Best-effort: returns False if the task isn't currently in memory.
        """
        if self._pipeline is None:
            return False
        task = self._pipeline.get_task(task_id)
        if task is None:
            return False
        task.agent_context.setdefault("injections", []).append(
            {"message": message, "ts": time.time(), "source": "dashboard"}
        )
        await self._publish_task_event("injected", task)
        return True

    _CONTROL_SIGNALS = {"paused": "pause", "running": "resume", "stopped": "stop"}
    # Control value → the run state the snapshot should show when there is no
    # live executor to honor the polled flag.
    _CONTROL_PERSISTED_STATE = {
        "stopped": "cancelled",
        "paused": "paused",
        "running": "running",
        "resume": "running",
    }

    async def set_run_control(self, task_id: str, value: str) -> bool:
        """Set a run's control (pause/resume/stop) on BOTH backends.

        Writes the Redis flag the in-process BlueprintExecutor polls between
        stages, AND sends the matching durable Signal to the Temporal workflow
        (a no-op when the in-process backend is active). One of the two is
        honored depending on the active provider — so the dashboard button works
        the same either way.

        When NO live executor is processing the run in-memory (e.g. the api
        restarted, leaving a "running" snapshot, or the run belongs to the
        legacy orchestrator store the Fleet list reads), the polled flag/Signal
        is never read — so we also reflect the control directly onto the
        persisted snapshot. Without this, Stop on such a run looked like a no-op.
        """
        applied = False
        sm = self.state_manager
        setter = getattr(sm, "set_pipeline_control", None)
        if sm is not None and setter is not None:
            await setter(task_id, value)
            applied = True
        sig = self._CONTROL_SIGNALS.get(value)
        live = self._pipeline is not None and self._pipeline.get_task(task_id) is not None
        if sig and self._pipeline is not None:
            if await self._pipeline.signal_run(task_id, sig):
                applied = True
        if not live:
            if await self._reflect_control_persisted(task_id, value):
                applied = True
        return applied

    async def _reflect_control_persisted(self, task_id: str, value: str) -> bool:
        """Write the control's target state onto the persisted snapshot.

        Handles both the pipeline-task store (`state` field) and the legacy
        orchestrator run store (`stage` field). Returns True if a snapshot was
        updated."""
        sm = self.state_manager
        target = self._CONTROL_PERSISTED_STATE.get(value)
        if sm is None or target is None:
            return False
        get_pt = getattr(sm, "get_pipeline_task", None)
        if get_pt is not None:
            task = await get_pt(task_id)
            if task:
                task["state"] = target
                task["updated_at"] = time.time()
                await sm.persist_task(task, ttl=self.config.pipeline_task_ttl)
                return True
        upd = getattr(sm, "update_run_stage", None)
        get_run = getattr(sm, "get_run", None)
        if upd is not None and get_run is not None and await get_run(task_id):
            await upd(task_id, target)
            return True
        return False

    async def get_legacy_run(self, task_id: str) -> dict[str, Any] | None:
        """Best-effort read of a legacy orchestrator run (`devai:run:*`)."""
        sm = self.state_manager
        getter = getattr(sm, "get_run", None)
        if sm is None or getter is None:
            return None
        try:
            return await getter(task_id)
        except Exception:  # noqa: BLE001
            return None

    async def delete_run(self, task_id: str) -> bool:
        """Remove a run entirely — from the in-memory pipeline (stopping it
        first), the pipeline-task store, and the legacy orchestrator store —
        plus its control flag. Idempotent; safe on zombie / unknown runs."""
        if self._pipeline is not None and self._pipeline.get_task(task_id) is not None:
            try:
                await self.set_run_control(task_id, "stopped")
            except Exception:  # noqa: BLE001
                logger.debug("stop-before-delete failed for %s", task_id, exc_info=True)
            self._pipeline.remove_task(task_id)
        sm = self.state_manager
        if sm is not None:
            # Evict from the durable work queue first so the reaper can't
            # resurrect a deleted run (drops it from queue/processing/active +
            # clears its claim). No-op if it was never on the durable queue.
            ack = getattr(sm, "ack_task", None)
            if ack is not None:
                try:
                    await ack(task_id)
                except Exception:  # noqa: BLE001
                    logger.debug("ack_task on delete failed for %s", task_id, exc_info=True)
            for method in ("delete_pipeline_task", "delete_run"):
                fn = getattr(sm, method, None)
                if fn is not None:
                    try:
                        await fn(task_id)
                    except Exception:  # noqa: BLE001
                        logger.warning("%s failed for %s", method, task_id, exc_info=True)
            ctrl = getattr(sm, "set_pipeline_control", None)
            if ctrl is not None:
                try:
                    await ctrl(task_id, "")
                except Exception:  # noqa: BLE001
                    logger.debug("clear control flag failed for %s", task_id, exc_info=True)
        return True

    async def resume_from_failure(self, task_id: str) -> dict[str, Any]:
        """Continue a FAILED (or cancelled) run from where it left off.

        The complement of retrigger: retrigger replays the whole intent as a
        NEW run; this re-enqueues the SAME run — completed stages are skipped
        on replay (``stages_completed``), so execution picks up at the failed
        stage with all its context (epic, stories, PR, branch, heal history)
        intact. The recovery agent gets a fresh round budget.
        """
        snapshot = await self.get_task(task_id)
        if snapshot is None:
            raise ValueError(f"run {task_id!r} not found")
        state = str(snapshot.get("state") or "")
        resumable = {"failed", "stage_failed", "agent_timeout", "cancelled"}
        if state not in resumable:
            raise ValueError(f"run is {state!r} — only failed/cancelled runs can be resumed")
        # Rate limit: one resume per run per cooldown window. Beyond the SADD
        # queue guard (which only dedupes concurrent enqueues), this stops a
        # retry-happy client or script from re-burning the run's LLM budget
        # in a tight loop — each resume re-executes real stages.
        redis = getattr(self.state_manager, "redis", None)
        if redis is not None:
            try:
                fresh = await redis.set(f"devai:pipeline:resume:{task_id}", "1", nx=True, ex=30)
            except Exception:  # noqa: BLE001 — rate limiting must not block recovery when Redis hiccups
                fresh = True
            if not fresh:
                raise ValueError(f"run {task_id!r} was resumed moments ago — wait before retrying")

        snapshot["state"] = "queued"
        snapshot["error"] = ""
        snapshot["failed_stage"] = ""
        snapshot["current_stage"] = ""
        snapshot["updated_at"] = time.time()
        ctx = snapshot.setdefault("agent_context", {})
        ctx["resumed_from_failure_at"] = time.time()
        sm = self.state_manager
        # Deliberate terminal→queued transition — force past the zombie guard.
        await sm.persist_task(snapshot, ttl=self.config.pipeline_task_ttl, force=True)
        # Evict the stale TERMINAL task from the in-process cache — without
        # this the worker's `self._tasks.get(task_id)` returns the old
        # failed object, sees is_terminal, and acks the run away without
        # executing anything (Continue would silently do nothing).
        if self._pipeline is not None:
            try:
                self._pipeline.remove_task(task_id)
            except Exception:  # noqa: BLE001
                logger.debug("cache evict failed for %s", task_id, exc_info=True)
        # A lingering "stopped" control flag would cancel the run at claim.
        ctrl = getattr(sm, "set_pipeline_control", None)
        if ctrl is not None:
            try:
                await ctrl(task_id, "")
            except Exception:  # noqa: BLE001
                logger.debug("control clear failed for %s", task_id, exc_info=True)
        enqueued = await sm.enqueue_task(task_id)
        if not enqueued:
            # Terminal runs were ack'd off the queue, so membership in the
            # ACTIVE set is leftover state (e.g. a crashed worker never
            # ack'd). Clear it and enqueue for real — silently reporting
            # "resumed" without a queue entry is the one unacceptable outcome.
            try:
                await sm.redis.srem(sm.PIPELINE_ACTIVE_KEY, task_id)
            except Exception:  # noqa: BLE001
                logger.debug("active-set clear failed for %s", task_id, exc_info=True)
            enqueued = await sm.enqueue_task(task_id)
        logger.info("run %s resumed from failure (was %s, enqueued=%s)", task_id, state, enqueued)
        return {"run_id": task_id, "state": "queued", "resumed": True, "was": state}

    async def approve_gate(
        self, task_id: str, gate: str, decision: str, *, approver: dict[str, Any] | None = None
    ) -> bool:
        """Approve or reject a human-approval gate on BOTH backends.

        `decision` is "approved" | "rejected". Records a Redis gate marker (for
        the in-process gate check + the dashboard's pending list) and sends the
        durable approve/reject Signal to the Temporal workflow.

        ``approver`` — the principal that made the decision (``{email, uid}``).
        Persisted alongside the decision so the human-in-the-loop guardrail has
        an auditable record of who approved/rejected. None means the legacy
        unattributed path (only reachable when auth is off).
        """
        sm = self.state_manager
        redis = getattr(sm, "redis", None)
        if redis is not None:
            try:
                await redis.set(f"devai:pipeline:gate:{task_id}:{gate}", decision, ex=86400)
                if approver:
                    record = {
                        "decision": decision,
                        "by": approver.get("email") or approver.get("uid") or "",
                        "uid": approver.get("uid", ""),
                        "ts": time.time(),
                    }
                    await redis.set(
                        f"devai:pipeline:gate:{task_id}:{gate}:approver",
                        json.dumps(record),
                        ex=86400,
                    )
            except Exception:  # noqa: BLE001
                logger.debug("gate marker write failed", exc_info=True)
        if self._pipeline is not None:
            await self._pipeline.signal_run(task_id, "approve" if decision == "approved" else "reject", [gate])
        return True

    async def list_gates(self, task_id: str) -> list[dict[str, Any]]:
        """List a run's approval gates with decision/pending status + the
        context a human needs to decide.

        A gate is PENDING only when the run has actually REACHED it (the
        stage has a STARTED event / is the current stage) and no decision is
        recorded. The old check ("no decision and not completed yet") marked
        every gate pending from the moment the run was created — users were
        prompted to approve deploy-release before a single stage had run.
        """
        snapshot = await self.get_task(task_id)
        if snapshot is None or self._pipeline is None:
            return []
        try:
            bp = self._pipeline.get_blueprint(snapshot.get("blueprint", ""))
        except Exception:  # noqa: BLE001
            return []
        redis = getattr(self.state_manager, "redis", None)

        completed = set(snapshot.get("stages_completed") or [])
        failed = set(snapshot.get("stages_failed") or [])
        skipped = set(snapshot.get("stages_skipped") or [])
        current = snapshot.get("current_stage") or ""
        events = snapshot.get("stage_events") or []
        started_at: dict[str, float] = {}
        last_message: dict[str, str] = {}
        for e in events:
            stage = e.get("stage", "")
            if e.get("phase") == "started" and stage not in started_at:
                started_at[stage] = e.get("timestamp") or 0.0
            if e.get("message"):
                last_message[stage] = e.get("message", "")

        out: list[dict[str, Any]] = []
        for s in bp.stages:
            if not getattr(s, "gate", False):
                continue
            decision = None
            if redis is not None:
                decision = await redis.get(f"devai:pipeline:gate:{task_id}:{s.name}")
            reached = s.name == current or s.name in started_at
            resolved = s.name in completed or s.name in failed or s.name in skipped
            out.append(
                {
                    "gate": s.name,
                    "title": s.display_title(),
                    "decision": decision,
                    "pending": decision is None and reached and not resolved,
                    # ── Context for the approval banner ──────────────
                    "agent": s.resolved_agent(),
                    "lane": s.lane,
                    "stage_type": s.type,
                    "blueprint": snapshot.get("blueprint", ""),
                    "repo": snapshot.get("repo", ""),
                    "intent": (snapshot.get("intent") or "")[:200],
                    "pr_number": snapshot.get("pr_number"),
                    "requested_at": started_at.get(s.name),
                    "summary": last_message.get(s.name, ""),
                    "stages_done": len(completed),
                }
            )

        # Dynamic gates — raised at runtime by stages that need human input
        # (plan_approval, or any future stage). Each carries its own context
        # (questions, plan summary, tech stack) for the approval banner.
        for dyn in (snapshot.get("agent_context") or {}).get("dynamic_gates") or []:
            if not isinstance(dyn, dict) or not dyn.get("gate"):
                continue
            decision = None
            if redis is not None:
                decision = await redis.get(f"devai:pipeline:gate:{task_id}:{dyn['gate']}")
            out.append(
                {
                    **dyn,
                    "decision": decision,
                    "pending": decision is None and TaskState(snapshot.get("state", "pending")) not in TERMINAL_STATES,
                    "blueprint": snapshot.get("blueprint", ""),
                    "repo": snapshot.get("repo", ""),
                    "stages_done": len(completed),
                }
            )
        return out

    async def list_persisted_tasks(
        self, *, limit: int = 50, blueprint: str | None = None, repo: str | None = None
    ) -> list[dict[str, Any]]:
        if self.state_manager is None:
            return []
        return await self.state_manager.list_pipeline_tasks(limit=limit, blueprint=blueprint, repo=repo)

    async def get_persisted_task(self, task_id: str) -> dict[str, Any] | None:
        if self.state_manager is None:
            return None
        return await self.state_manager.get_pipeline_task(task_id)

    async def get_task(self, task_id: str) -> dict[str, Any] | None:
        """Best-effort: prefer the in-memory snapshot, fall back to Redis."""
        in_mem = self.get_task_in_memory(task_id)
        if in_mem is not None:
            return in_mem
        return await self.get_persisted_task(task_id)

    # ── SSE event stream ────────────────────────────────────────────

    def recent_events(self, limit: int = 200) -> list[dict[str, Any]]:
        return [{"timestamp": ts, "task_id": tid, **payload} for ts, tid, payload in list(self._ring)[-limit:]]

    async def event_stream(
        self, *, replay: int = 0, heartbeat_seconds: float = 0.0
    ) -> AsyncIterator[tuple[float, str, dict[str, Any] | None]]:
        """Async generator yielding live run events (typed envelopes).

        `replay` — emit the last N events from the ring buffer before
        switching to live. Useful for SSE reconnects with Last-Event-ID.

        `heartbeat_seconds` — when > 0, yield a `(now, "", None)` sentinel
        after that much silence so the SSE route can emit a keep-alive
        comment (proxy idle timeouts) and notice disconnects promptly. The
        timeout lives HERE (around our own queue.get) because cancelling
        the generator's __anext__ from outside would tear the generator
        down on the first quiet period.
        """
        queue: asyncio.Queue[tuple[float, str, dict[str, Any]]] = asyncio.Queue()
        async with self._lock:
            self._sse_queues.append(queue)

        # Replay first
        if replay:
            for entry in list(self._ring)[-replay:]:
                yield entry

        try:
            while True:
                if heartbeat_seconds > 0:
                    try:
                        yield await asyncio.wait_for(queue.get(), timeout=heartbeat_seconds)
                    except TimeoutError:
                        yield (time.time(), "", None)
                else:
                    yield await queue.get()
        finally:
            async with self._lock:
                if queue in self._sse_queues:
                    self._sse_queues.remove(queue)

    # ── Internal: per-event fan-out ──────────────────────────────────

    def _on_event(self, task: DevAITask, event: StageEvent) -> None:
        """Called by Pipeline / BlueprintExecutor for every stage event.

        The run-event spine. For every stage transition we:
          1. Derive the live-observability signals (per-agent status,
             supervisor/orchestrator synthesis + progress, coordination A2A)
             onto the task — see _derive_run_signals.
          2. Append the stage envelope + derived envelopes to the ring
             buffer (SSE replay) and every subscribed SSE queue.
          3. Append the envelopes to the durable per-run Redis logs
             (devai:run:{id}:events / :a2a_messages) and schedule a
             persist_task snapshot (best-effort, fire-and-forget).
          4. Publish to NATS via the event-bus adapter so any external
             subscriber (legacy agents, dashboards, downstream services)
             sees stage transitions and terminal state changes.
        Every sink is individually fenced — one failing must not take the
        others (or the run) down.
        """
        ts = event.timestamp
        payload = {
            "event_type": "stage",
            "stage": event.stage,
            "phase": event.phase.value,
            "duration_ms": event.duration_ms,
            "message": event.message,
            "error": event.error,
            "agent": event.agent,
            "lane": event.lane,
            "task_state": task.state.value,
            "blueprint": task.blueprint,
            "repo": task.repo,
        }

        # 1. Derivations mutate the task (agents dict, a2a, routing) BEFORE
        #    the snapshot persists, so polling readers see the same truth as
        #    SSE subscribers.
        try:
            derived = self._derive_run_signals(task, event, ts)
        except Exception:  # noqa: BLE001 — derivation must never break the run
            logger.exception("run-signal derivation failed for %s", task.id)
            derived = []

        self._ring.append((ts, task.id, payload))
        for env in derived:
            self._ring.append((ts, task.id, env))

        # Best-effort OTel: one metric per terminal stage transition. record_*
        # never raises; guard the rare case telemetry is wired but malformed.
        if self.telemetry is not None and event.phase.value in _TELEMETRY_STAGE_PHASES:
            try:
                from devai.adapters.telemetry import StageMetric

                self.telemetry.record_stage(
                    StageMetric(
                        blueprint=task.blueprint or "",
                        stage=event.stage or "",
                        status=event.phase.value,
                        duration_ms=float(event.duration_ms or 0.0),
                        repo=task.repo or "",
                        # Per-fleet attribution — the agent persona + lane on
                        # the event light up per-agent metric breakdowns.
                        agent=event.agent or "",
                        lane=event.lane or "",
                        run_id=task.id,
                    )
                )
            except Exception:  # noqa: BLE001
                logger.debug("telemetry record_stage failed", exc_info=True)

        for q in list(self._sse_queues):
            try:
                q.put_nowait((ts, task.id, payload))
                for env in derived:
                    q.put_nowait((ts, task.id, env))
            except asyncio.QueueFull:
                logger.warning("SSE queue full — dropping event")

        # Dry-run tasks are never persisted — a preview must leave no trace.
        if self.state_manager is not None and not task.dry_run:
            try:
                asyncio.create_task(self.state_manager.persist_task(task.to_dict(), ttl=self.config.pipeline_task_ttl))
            except RuntimeError:
                # No running loop (test context) — skip persistence.
                pass
            # Durable per-run logs: the event sequence + A2A messages survive
            # pod restarts (the in-proc ring does not). Capped + TTL'd.
            try:
                self._persist_run_log(task.id, [payload, *derived], ts)
            except RuntimeError:
                pass

        # Epic supervision: milestone progress lands as comments on the run's
        # epic issue so the epic is the single supervised thread tying every
        # story, PR, and stage outcome together.
        try:
            self._epic_progress(task, event)
        except Exception:  # noqa: BLE001 — supervision must never break the run
            logger.exception("epic progress sink failed for %s", task.id)

        # Mirror to NATS. Subject layout:
        #   devai.pipeline.stage.<stage>.<phase>   per-stage progress
        #   devai.pipeline.task.<completed|failed> on terminal transitions
        # Failure-mode: NATS goes down → publish raises → we log + carry on.
        # The pipeline must never crash because the broker is gone.
        try:
            self._schedule_publish(
                f"devai.pipeline.stage.{event.stage}.{event.phase.value}",
                {"task_id": task.id, **payload, "timestamp": ts},
            )
            # Mirror the derived envelopes so JetStream stays the canonical
            # durable record of agent activity + coordination traffic.
            for env in derived:
                if env.get("event_type") == "a2a":
                    self._schedule_publish(f"devai.pipeline.a2a.{task.id}", {"task_id": task.id, **env})
                elif env.get("event_type") == "agent_status":
                    self._schedule_publish(
                        f"devai.pipeline.agent.{env.get('agent', 'unknown')}.{env.get('status', 'unknown')}",
                        {"task_id": task.id, **env},
                    )
        except RuntimeError:
            pass  # no running loop

        if task.state in TERMINAL_STATES and event.phase in (
            StageEventPhase.COMPLETED,
            StageEventPhase.FAILED,
        ):
            kind = "failed" if task.state in FAILURE_STATES else "completed"
            try:
                self._schedule_publish(
                    f"devai.pipeline.task.{kind}",
                    self._task_event_payload(kind, task),
                )
            except RuntimeError:
                pass

    # ── Internal: NATS publish helpers ───────────────────────────────

    def _task_event_payload(self, kind: str, task: DevAITask) -> dict[str, Any]:
        return {
            "kind": kind,
            "task_id": task.id,
            "blueprint": task.blueprint,
            "repo": task.repo,
            "intent": task.intent,
            "trigger_type": task.trigger_type,
            "state": task.state.value,
            "current_stage": task.current_stage,
            "stages_completed": list(task.stages_completed),
            "stages_failed": list(task.stages_failed),
            "error": task.error,
            "failed_stage": task.failed_stage,
            "pr_number": task.pr_number,
            "issue_number": task.issue_number,
            "branch_name": task.branch_name,
            "triggered_by": task.triggered_by,
            "trace_id": task.trace_id,
            "preview_url": task.agent_context.get("preview_url"),
            "editor_url": task.agent_context.get("editor_url"),
            "timestamp": time.time(),
        }

    async def _publish_task_event(self, kind: str, task: DevAITask) -> None:
        """Publish a task-level event (`created`, `completed`, `failed`)."""
        adapter = self.event_bus_adapter
        if adapter is None:
            return
        try:
            await adapter.publish(
                f"devai.pipeline.task.{kind}",
                self._task_event_payload(kind, task),
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "event_bus publish failed: subject=devai.pipeline.task.%s task=%s",
                kind,
                task.id,
                exc_info=True,
            )

    # ── Internal: live-observability derivations (run-event spine) ────

    #: stage phase → agent card status
    _PHASE_TO_STATUS = {
        "started": "running",
        "completed": "completed",
        "failed": "failed",
        "skipped": "skipped",
    }
    _A2A_CAP = 500  # max coordination messages kept per run

    def _derive_run_signals(self, task: DevAITask, event: StageEvent, ts: float) -> list[dict[str, Any]]:
        """Derive agent status, coordination synthesis, and A2A from one
        stage event. Mutates the task; returns the extra SSE envelopes.

        This is the blueprint-runtime counterpart of what the legacy
        LangGraph agents did imperatively (set_agent_status + A2ABus) —
        derived once here so every stage type (agentic, deterministic,
        job-runner, crew) gets live observability for free.
        """
        envelopes: list[dict[str, Any]] = []
        phase = event.phase.value
        status = self._PHASE_TO_STATUS.get(phase, "")
        agent = (event.agent or "").strip()

        # ── 1. Per-agent runtime status ─────────────────────────────
        if agent and status:
            entry: dict[str, Any] = {"status": status, "updated_at": ts, "stage": event.stage}
            if event.error:
                entry["error"] = event.error
            task.agents[agent] = entry
            envelopes.append(
                {
                    "event_type": "agent_status",
                    "agent": agent,
                    "status": status,
                    "stage": event.stage,
                    "error": event.error,
                    "blueprint": task.blueprint,
                }
            )
            # Mirror to the legacy Redis hash so anything still reading
            # devai:run:{id}:agents (older dashboard surfaces) agrees.
            if self.state_manager is not None and not task.dry_run:
                try:
                    self._spawn(self.state_manager.set_agent_status(task.id, agent, status, event.error))
                except RuntimeError:
                    pass

        # ── 2. Coordination layer synthesis ─────────────────────────
        # The supervisor/orchestrator cards represent the runtime itself:
        # running while the task runs, terminal with the task. A blueprint
        # with a REAL supervisor stage overwrites these via branch 1.
        terminal = task.state in TERMINAL_STATES
        coord_status = ("failed" if task.state in FAILURE_STATES else "completed") if terminal else "running"
        for coord in ("supervisor", "orchestrator"):
            prev = task.agents.get(coord, {}).get("status")
            if prev != coord_status:
                task.agents[coord] = {"status": coord_status, "updated_at": ts, "stage": event.stage}
                envelopes.append(
                    {
                        "event_type": "agent_status",
                        "agent": coord,
                        "status": coord_status,
                        "stage": event.stage,
                        "error": None,
                        "blueprint": task.blueprint,
                    }
                )

        # Progress: completed stages over the blueprint's total. Falls back
        # to the stages seen so far when the blueprint isn't loaded.
        total = self._blueprint_stage_count(task.blueprint)
        seen = len(task.stages_completed) + len(task.stages_skipped)
        denominator = total or (seen + len(task.stages_failed) + (1 if task.current_stage else 0)) or 1
        progress_pct = min(100, round(100 * seen / denominator))
        task.agent_context["orchestrator_routing"] = {
            "progress_pct": 100 if terminal and not task.error else progress_pct,
            "current_phase": event.lane or "",
            "status_summary": f"{event.stage} {phase}" + (f" — {event.error}" if event.error else ""),
            "decision": task.state.value,
        }

        # ── 3. Coordination A2A (handoff / response / escalation) ───
        # Only for persona-bearing stages; deterministic plumbing stages
        # stay visible via the Events tab without flooding the timeline.
        if agent and phase in ("started", "completed", "failed"):
            if phase == "started":
                msg_type, frm, to = "handoff", "supervisor", agent
                subject = f"Stage '{event.stage}' handed to {agent}"
                body = event.message or f"{event.stage} started"
            elif phase == "completed":
                msg_type, frm, to = "response", agent, "supervisor"
                subject = f"'{event.stage}' completed in {event.duration_ms / 1000:.1f}s"
                body = event.message or "done"
            else:
                msg_type, frm, to = "escalation", agent, "supervisor"
                subject = f"'{event.stage}' FAILED"
                body = event.error or "unknown error"

            a2a = {
                "id": uuid.uuid4().hex,
                "from_agent": frm,
                "to_agent": to,
                "message_type": msg_type,
                "subject": subject,
                "body": body[:1000],
                "payload": {"stage": event.stage, "phase": phase, "duration_ms": event.duration_ms},
                "in_reply_to": None,
                "timestamp": ts,
                "triggered_by": task.triggered_by or "",
                "trace_id": task.trace_id or task.id,
            }
            msgs = task.agent_context.setdefault("a2a_messages", [])
            if isinstance(msgs, list):
                msgs.append(a2a)
                if len(msgs) > self._A2A_CAP:
                    del msgs[: len(msgs) - self._A2A_CAP]
            envelopes.append({"event_type": "a2a", **a2a})

        return envelopes

    #: Stages whose completion/failure is worth a supervised epic comment.
    _EPIC_MILESTONES = frozenset(
        {
            "create-plan",
            "plan-approval",
            "implement-code",
            "db-engineering",
            "review-code",
            "security-scan",
            "monitor-build",
            "run-tests",
            "diagnose-test-failures",
            "fix-test-failures",
            "rerun-tests",
            "staff-review",
            "provision-infra",
            "deploy-release",
        }
    )

    def _epic_progress(self, task: DevAITask, event: StageEvent) -> None:
        """Post supervised progress to the run's epic issue (best-effort).

        One comment per milestone stage outcome + a final summary with a
        devai:done / devai:failed label swap at terminal state. Posted-keys
        are tracked on the task itself so resumes don't double-comment.
        """
        if self.scm is None or task.dry_run or not task.epic_issue_number or not task.repo:
            return
        phase = event.phase.value
        if phase not in ("completed", "failed"):
            return
        terminal = task.state in TERMINAL_STATES
        if event.stage not in self._EPIC_MILESTONES and not terminal:
            return

        posted = task.agent_context.setdefault("epic_progress_posted", [])
        marker = f"{event.stage}:{phase}" if not terminal else "terminal"
        if not isinstance(posted, list) or marker in posted:
            return
        posted.append(marker)

        repo, epic = task.repo, task.epic_issue_number
        scm = self.scm

        if terminal:
            ok = task.state not in FAILURE_STATES
            status = "✅ completed" if ok else f"❌ {task.state.value}"
            lines = [
                f"## Pipeline run {status}",
                "",
                f"**Run:** `{task.id}`  ·  **Blueprint:** `{task.blueprint}`",
                f"**Stages:** {len(task.stages_completed)} completed"
                + (f", {len(task.stages_failed)} failed" if task.stages_failed else ""),
            ]
            if task.pr_number:
                lines.append(f"**Pull request:** #{task.pr_number}")
            if task.error:
                lines.append(f"**Error:** {task.error[:300]}")
            body = "\n".join(lines)
            label = "devai:done" if ok else "devai:failed"

            async def _post_terminal() -> None:
                try:
                    await scm.add_comment(repo, epic, body)
                    await scm.add_labels(repo, epic, [label])
                except Exception:  # noqa: BLE001
                    logger.debug("epic terminal comment failed for #%s", epic, exc_info=True)

            try:
                self._spawn(_post_terminal())
            except RuntimeError:
                pass
            return

        icon = "✅" if phase == "completed" else "❌"
        title = event.stage.replace("-", " ").replace("_", " ").title()
        parts = [f"{icon} **{title}**"]
        if event.agent:
            parts.append(f"by `{event.agent}`")
        if event.duration_ms:
            parts.append(f"in {event.duration_ms / 1000:.1f}s")
        line = " ".join(parts)
        if event.message:
            line += f"\n> {event.message[:300]}"
        if event.error:
            line += f"\n> ⚠️ {event.error[:300]}"
        if event.stage == "implement-code" and task.pr_number:
            line += f"\n\nOpened pull request #{task.pr_number}."

        async def _post() -> None:
            try:
                await scm.add_comment(repo, epic, line)
            except Exception:  # noqa: BLE001
                logger.debug("epic progress comment failed for #%s", epic, exc_info=True)

        try:
            self._spawn(_post())
        except RuntimeError:
            pass

    def _blueprint_stage_count(self, name: str) -> int:
        if not name or self._pipeline is None:
            return 0
        cached = self._bp_stage_counts.get(name)
        if cached is not None:
            return cached
        try:
            bp = self._pipeline.get_blueprint(name)
            count = len(bp.stages) if bp is not None else 0
        except Exception:  # noqa: BLE001
            count = 0
        self._bp_stage_counts[name] = count
        return count

    def _persist_run_log(self, task_id: str, envelopes: list[dict[str, Any]], ts: float) -> None:
        """Append envelopes to the durable per-run Redis logs.

        devai:run:{id}:events       — every envelope (stage/agent_status/a2a)
        devai:run:{id}:a2a_messages — a2a only (same key the legacy LangGraph
                                      path used, so existing readers work)
        Both capped and TTL'd by pipeline_task_ttl. Fire-and-forget.
        """
        redis = getattr(self.state_manager, "redis", None)
        if redis is None:
            return
        ttl = int(getattr(self.config, "pipeline_task_ttl", 86400 * 30))

        async def _write() -> None:
            try:
                pipe = redis.pipeline()
                events_key = f"devai:run:{task_id}:events"
                for env in envelopes:
                    pipe.rpush(events_key, json.dumps({"timestamp": ts, "task_id": task_id, **env}, default=str))
                pipe.ltrim(events_key, -2000, -1)
                pipe.expire(events_key, ttl)
                a2a_key = f"devai:run:{task_id}:a2a_messages"
                a2a_envs = [e for e in envelopes if e.get("event_type") == "a2a"]
                if a2a_envs:
                    for env in a2a_envs:
                        pipe.rpush(
                            a2a_key, json.dumps({k: v for k, v in env.items() if k != "event_type"}, default=str)
                        )
                    pipe.ltrim(a2a_key, -self._A2A_CAP, -1)
                    pipe.expire(a2a_key, ttl)
                await pipe.execute()
            except Exception:  # noqa: BLE001
                logger.debug("run-log persist failed for %s", task_id, exc_info=True)

        self._spawn(_write())

    def _spawn(self, coro: Any) -> None:
        """Fire-and-forget a coroutine with a strong ref (GC-safe)."""
        task = asyncio.create_task(coro)
        self._publish_tasks.add(task)
        task.add_done_callback(self._publish_tasks.discard)

    def _schedule_publish(self, subject: str, payload: dict[str, Any]) -> None:
        """Fire-and-forget publish from sync context (the _on_event hook
        is sync). Schedules a task on the running loop; raises RuntimeError
        when called outside one so the caller can no-op."""
        adapter = self.event_bus_adapter
        if adapter is None:
            return

        async def _pub() -> None:
            try:
                await adapter.publish(subject, payload)
            except Exception:  # noqa: BLE001
                logger.warning("event_bus publish failed: subject=%s", subject, exc_info=True)

        task = asyncio.create_task(_pub())
        # Keep a strong ref until the task completes — otherwise the GC may
        # drop the only reference and the NATS publish is silently cancelled.
        self._publish_tasks.add(task)
        task.add_done_callback(self._publish_tasks.discard)

    def _ensure_started(self) -> None:
        if not self._started or self._pipeline is None:
            raise RuntimeError("PipelineService is not started — call await svc.start() first")


__all__ = ["PipelineService"]
