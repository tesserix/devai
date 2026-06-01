"""Core data types for the Fiber-style pipeline runtime.

Mirrors `internal/pipeline/types.go` from the Fiber reference design with
Python conventions. The intent is that the same conceptual primitives
(Task, TaskState, StageResult, StageEvent) exist on both sides so the
mental model translates 1:1.

Nothing in this module touches the database or LLM providers — it's the
plain data model that everything else operates on.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class TaskState(str, Enum):
    """Lifecycle states a DevAITask can move through.

    Fiber's vocabulary, adapted to the ALM + SRE domain. The happy path
    for the alm-pipeline blueprint is:

        pending → queued → ingesting → analyzing → planning
              → implementing → reviewing → security_scanning
              → building → testing → provisioning → deploying → completed

    Branches:
        SRE monitor:  pending → queued → discovering → monitoring → correlating → responding → learning → completed
        PR review:    pending → queued → reviewing → security_scanning → completed
    """

    # Entry
    PENDING = "pending"
    QUEUED = "queued"

    # ALM planning
    INGESTING = "ingesting"
    ANALYZING = "analyzing"
    PLANNING = "planning"

    # ALM implementation
    IMPLEMENTING = "implementing"

    # Quality gates
    REVIEWING = "reviewing"
    SECURITY_SCANNING = "security_scanning"
    BUILDING = "building"
    TESTING = "testing"

    # Deployment
    PROVISIONING = "provisioning"
    DEPLOYING = "deploying"

    # SRE
    DISCOVERING = "discovering"
    MONITORING = "monitoring"
    CORRELATING = "correlating"
    RESPONDING = "responding"
    LEARNING = "learning"

    # Generic stage-in-progress (used by blueprint stages that don't map to a
    # specific lifecycle state; the executor still tracks per-stage events)
    RUNNING = "running"

    # Terminal — success
    COMPLETED = "completed"

    # Terminal — failure
    FAILED = "failed"
    STAGE_FAILED = "stage_failed"
    AGENT_TIMEOUT = "agent_timeout"
    BUDGET_EXCEEDED = "budget_exceeded"
    CANCELLED = "cancelled"

    # Terminal — human gate
    AWAITING_APPROVAL = "awaiting_approval"
    AWAITING_HUMAN_INPUT = "awaiting_human_input"
    PAUSED = "paused"
    HUMAN_TAKEOVER = "human_takeover"


TERMINAL_STATES: frozenset[TaskState] = frozenset(
    {
        TaskState.COMPLETED,
        TaskState.FAILED,
        TaskState.STAGE_FAILED,
        TaskState.AGENT_TIMEOUT,
        TaskState.BUDGET_EXCEEDED,
        TaskState.CANCELLED,
    }
)

FAILURE_STATES: frozenset[TaskState] = frozenset(
    {
        TaskState.FAILED,
        TaskState.STAGE_FAILED,
        TaskState.AGENT_TIMEOUT,
        TaskState.BUDGET_EXCEEDED,
    }
)

HUMAN_GATE_STATES: frozenset[TaskState] = frozenset(
    {
        TaskState.AWAITING_APPROVAL,
        TaskState.AWAITING_HUMAN_INPUT,
        TaskState.PAUSED,
        TaskState.HUMAN_TAKEOVER,
    }
)


class StageEventPhase(str, Enum):
    """The transition a stage just made.

    Emitted on the EventBus so the dashboard timeline can render per-stage
    wall-clock bars.
    """

    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(slots=True)
class StageEvent:
    """Per-stage telemetry record.

    The executor emits one StageEvent per (stage, phase) pair. Persisted on
    the task (for reload), broadcast on the EventBus (for SSE), and mirrored
    to JetStream once the natsbridge equivalent is wired up.
    """

    stage: str
    phase: StageEventPhase
    timestamp: float = field(default_factory=time.time)
    duration_ms: float = 0.0
    message: str = ""
    error: str | None = None
    # Stage render hints, copied from the StageSpec. Let SSE / NATS consumers
    # label a node (gate? what kind of stage?) without re-reading the blueprint.
    stage_type: str = ""
    gate: bool = False
    # When this event marks a git checkpoint (an agent step the user can roll
    # back to), `checkpoint` carries the commit SHA. The dashboard renders
    # these as entries on the right-rail checkpoint timeline.
    checkpoint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "phase": self.phase.value,
            "timestamp": self.timestamp,
            "duration_ms": self.duration_ms,
            "message": self.message,
            "error": self.error,
            "type": self.stage_type,
            "gate": self.gate,
            "checkpoint": self.checkpoint,
        }


@dataclass(slots=True)
class StageResult:
    """The structured return value from PipelineStage.execute().

    `next_state` is advisory — the executor uses it to drive the task state
    machine, but a stage that doesn't care about the lifecycle state can
    leave it None and the executor will hold the current state.

    `data` is merged into `task.agent_context` and becomes available to
    downstream stages (this is the handover mechanism). Keys should be
    namespaced by role (e.g. `requirements_analyst_output`).
    """

    next_state: TaskState | None = None
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class DevAITask:
    """The unit of work flowing through the pipeline.

    Mirrors Fiber's FiberTask. One DevAITask = one pipeline run = one
    blueprint execution. Stages mutate `agent_context` (the handover bag)
    and `stage_events` (the timeline); the executor mutates `state`,
    `stages_completed`, and the failure fields.
    """

    # Identity
    id: str = field(default_factory=lambda: f"devai-{uuid.uuid4().hex[:12]}")
    intent: str = ""
    blueprint: str = "alm-pipeline"
    repo: str = ""
    trigger_type: str = "manual"  # manual | webhook | cron | slack

    # State
    state: TaskState = TaskState.PENDING
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None

    # Execution tracking
    stages_completed: list[str] = field(default_factory=list)
    stages_skipped: list[str] = field(default_factory=list)
    stages_failed: list[str] = field(default_factory=list)
    stage_events: list[StageEvent] = field(default_factory=list)
    current_stage: str = ""

    # The handover bag. Stages write outputs here; downstream stages read.
    # Keys are role-namespaced: `requirements_analyst_output`, `coder_output`,
    # `staff_reviewer_output`, etc.
    agent_context: dict[str, Any] = field(default_factory=dict)

    # Linkage into the existing world (filled in by adapter stages)
    issue_number: int | None = None
    pr_number: int | None = None
    branch_name: str | None = None
    epic_issue_number: int | None = None
    story_issue_numbers: list[int] = field(default_factory=list)
    sandbox_pod: str | None = None
    dev_server_port: int | None = None

    # Failure context (populated when state ∈ FAILURE_STATES)
    error: str | None = None
    failed_stage: str | None = None

    # Optional human-readable label that the dashboard shows
    label: str = ""

    # Git checkpoints captured during the run (the Cursor "roll back to a
    # previous snapshot" timeline). Each entry: {sha, label, stage, ts}. The
    # runner pod emits CHECKPOINT:: lines; job_watcher parses them onto here.
    checkpoints: list[dict[str, Any]] = field(default_factory=list)

    # ── Caller identity (filled in at the boundary by webhook / chat /
    # dashboard trigger). principal is the serialized devai.identity.Principal;
    # trace_id correlates with LangSmith / OTEL spans; triggered_by is the
    # convenience handle the dashboard renders alongside the task row.
    principal: dict[str, Any] | None = None
    trace_id: str | None = None
    triggered_by: str | None = None

    # ── Teams (Phase 2/3). team_id = the human team that owns this run;
    # crew_id = the AI agent crew assigned to it. Both default empty so
    # every pre-teams trigger path keeps working unscoped.
    team_id: str = ""
    crew_id: str = ""

    # ────── Conditional helpers ──────
    # The blueprint executor uses these for `condition:` expressions.
    # Keep them as @property so they always reflect current state.

    @property
    def has_pr(self) -> bool:
        return self.pr_number is not None

    @property
    def has_issue(self) -> bool:
        return self.issue_number is not None

    @property
    def has_sandbox(self) -> bool:
        return self.sandbox_pod is not None

    @property
    def has_epic(self) -> bool:
        return self.epic_issue_number is not None

    @property
    def has_stories(self) -> bool:
        return bool(self.story_issue_numbers)

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def is_failed(self) -> bool:
        return self.state in FAILURE_STATES

    # ────── Mutation helpers ──────

    def transition(self, new_state: TaskState) -> None:
        """Move to a new state and bump the timestamp.

        Doesn't enforce a state-graph — the orchestrator is responsible
        for not making nonsense transitions. This is intentional: blueprints
        define their own state ordering and we don't want to encode a global
        DAG into the type system.
        """
        self.state = new_state
        self.updated_at = time.time()
        if new_state == TaskState.QUEUED and self.started_at is None:
            self.started_at = time.time()
        if new_state in TERMINAL_STATES and self.finished_at is None:
            self.finished_at = time.time()

    def merge_handover(self, data: dict[str, Any]) -> None:
        """Merge a stage's StageResult.data into the handover bag.

        Shallow merge — by convention, each stage writes a single
        role-namespaced key, so collisions only happen when a stage runs
        twice (retry loop), which is the right behavior (latest wins).
        """
        if data:
            self.agent_context.update(data)
            self.updated_at = time.time()

    def record_event(self, event: StageEvent) -> None:
        self.stage_events.append(event)
        self.updated_at = time.time()

    def record_checkpoint(self, sha: str, label: str = "", stage: str = "") -> dict[str, Any]:
        """Append a git checkpoint (rollback point) to the timeline."""
        entry = {"sha": sha, "label": label, "stage": stage or self.current_stage, "ts": time.time()}
        self.checkpoints.append(entry)
        self.updated_at = time.time()
        return entry

    def to_dict(self) -> dict[str, Any]:
        """Serialize for persistence / SSE transmission."""
        return {
            "id": self.id,
            "intent": self.intent,
            "blueprint": self.blueprint,
            "repo": self.repo,
            "trigger_type": self.trigger_type,
            "state": self.state.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "stages_completed": list(self.stages_completed),
            "stages_skipped": list(self.stages_skipped),
            "stages_failed": list(self.stages_failed),
            "stage_events": [e.to_dict() for e in self.stage_events],
            "current_stage": self.current_stage,
            "agent_context": dict(self.agent_context),
            "issue_number": self.issue_number,
            "pr_number": self.pr_number,
            "branch_name": self.branch_name,
            "epic_issue_number": self.epic_issue_number,
            "story_issue_numbers": list(self.story_issue_numbers),
            "sandbox_pod": self.sandbox_pod,
            "dev_server_port": self.dev_server_port,
            "error": self.error,
            "failed_stage": self.failed_stage,
            "label": self.label,
            "checkpoints": list(self.checkpoints),
            "principal": dict(self.principal) if self.principal else None,
            "trace_id": self.trace_id,
            "triggered_by": self.triggered_by,
            "team_id": self.team_id,
            "crew_id": self.crew_id,
        }
