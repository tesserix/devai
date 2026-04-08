"""Shared data models for the DevAI ALM pipeline."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field
from ulid import ULID

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class PipelineStage(StrEnum):
    """Full ALM lifecycle stages."""

    TRIGGERED = "triggered"
    REQUIREMENTS_ANALYZED = "requirements_analyzed"
    EPIC_CREATED = "epic_created"
    STORIES_CREATED = "stories_created"
    PLAN_CREATED = "plan_created"
    CODE_IMPLEMENTED = "code_implemented"
    CODE_REVIEWED = "code_reviewed"
    REVIEW_CHANGES_REQUESTED = "review_changes_requested"
    BUILD_MONITORING = "build_monitoring"
    TESTS_COMPLETE = "tests_complete"
    DEPLOYING = "deploying"
    DEPLOYED = "deployed"
    DONE = "done"
    FAILED = "failed"


class TriggerType(StrEnum):
    GITHUB_ISSUE = "github_issue"
    PROJECT_CARD = "project_card"
    CLI = "cli"
    WEBHOOK = "webhook"


class ReviewDecision(StrEnum):
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"


class AgentRole(StrEnum):
    SUPERVISOR = "supervisor"
    ORCHESTRATOR = "orchestrator"
    REQUIREMENTS_ANALYST = "requirements_analyst"
    PRODUCT_DIRECTOR = "product_director"
    ENGINEERING_MANAGER = "engineering_manager"
    SENIOR_DEVELOPER = "senior_developer"
    STAFF_REVIEWER = "staff_reviewer"
    QA_TESTER = "qa_tester"
    CI_MONITOR = "ci_monitor"
    RELEASE_MANAGER = "release_manager"


class A2AMessageType(StrEnum):
    """Agent-to-Agent message types."""

    REQUEST = "request"
    RESPONSE = "response"
    NOTIFICATION = "notification"
    CLARIFICATION = "clarification"
    ESCALATION = "escalation"
    HANDOFF = "handoff"


class BuildStatus(StrEnum):
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    SUCCESS = "success"
    FAILURE = "failure"
    CANCELLED = "cancelled"


class DeploymentStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SUCCESS = "success"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


# ---------------------------------------------------------------------------
# A2A Communication
# ---------------------------------------------------------------------------


class A2AMessage(BaseModel):
    """Message exchanged between agents for inter-agent communication."""

    id: str = Field(default_factory=lambda: str(ULID()))
    from_agent: str
    to_agent: str
    message_type: A2AMessageType
    subject: str
    body: str
    payload: dict[str, Any] = Field(default_factory=dict)
    in_reply_to: str | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def reply(self, body: str, payload: dict[str, Any] | None = None) -> A2AMessage:
        """Create a reply to this message."""
        return A2AMessage(
            from_agent=self.to_agent,
            to_agent=self.from_agent,
            message_type=A2AMessageType.RESPONSE,
            subject=f"Re: {self.subject}",
            body=body,
            payload=payload or {},
            in_reply_to=self.id,
        )


# ---------------------------------------------------------------------------
# Agent Results
# ---------------------------------------------------------------------------


class AgentResult(BaseModel):
    agent_name: str
    status: str  # "success" | "needs_retry" | "failed"
    output: dict[str, Any] = Field(default_factory=dict)
    summary: str = ""
    duration_seconds: float = 0.0
    a2a_messages_sent: list[A2AMessage] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Domain Models
# ---------------------------------------------------------------------------


class Requirement(BaseModel):
    """Analyzed and refined requirement."""

    id: str = Field(default_factory=lambda: str(ULID()))
    title: str
    description: str
    stakeholder: str = ""
    priority: str = "medium"
    category: str = ""  # functional | non-functional | performance | security
    acceptance_criteria: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)


class Epic(BaseModel):
    """GitHub Epic (large issue that groups user stories)."""

    title: str
    description: str
    labels: list[str] = Field(default_factory=list)
    stories: list[str] = Field(default_factory=list)  # Story IDs
    issue_number: int | None = None
    url: str = ""


class UserStory(BaseModel):
    title: str
    description: str
    acceptance_criteria: list[str] = Field(default_factory=list)
    priority: str = "medium"
    labels: list[str] = Field(default_factory=list)
    epic_ref: str = ""  # Reference to parent epic


class TechnicalPlan(BaseModel):
    summary: str
    affected_files: list[str] = Field(default_factory=list)
    approach: str = ""
    subtasks: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    estimated_complexity: str = "medium"


class CodeReview(BaseModel):
    decision: ReviewDecision
    summary: str
    comments: list[dict[str, str]] = Field(default_factory=list)
    security_issues: list[str] = Field(default_factory=list)
    performance_issues: list[str] = Field(default_factory=list)
    style_issues: list[str] = Field(default_factory=list)


class BuildResult(BaseModel):
    """GitHub Actions build result."""

    run_id: int = 0
    workflow_name: str = ""
    status: BuildStatus = BuildStatus.QUEUED
    conclusion: str = ""
    url: str = ""
    started_at: str = ""
    completed_at: str = ""
    logs_url: str = ""
    failed_jobs: list[dict[str, str]] = Field(default_factory=list)


class TestResult(BaseModel):
    total: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    test_files: list[str] = Field(default_factory=list)
    failures: list[dict[str, str]] = Field(default_factory=list)
    screenshots: list[str] = Field(default_factory=list)


class DeploymentResult(BaseModel):
    """Deployment/release result."""

    status: DeploymentStatus = DeploymentStatus.PENDING
    environment: str = "production"
    version: str = ""
    commit_sha: str = ""
    deployed_at: str = ""
    argocd_app: str = ""
    health_check_url: str = ""
    health_check_passed: bool = False


# ---------------------------------------------------------------------------
# Pipeline Context
# ---------------------------------------------------------------------------


class PipelineContext(BaseModel):
    run_id: str = Field(default_factory=lambda: str(ULID()))
    repo_full_name: str
    trigger_type: TriggerType
    trigger_ref: str
    requirements: str
    stage: PipelineStage = PipelineStage.TRIGGERED
    branch_name: str | None = None
    pr_number: int | None = None
    review_iteration: int = 0
    artifacts: dict[str, Any] = Field(default_factory=dict)
    a2a_messages: list[A2AMessage] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def advance_stage(self, new_stage: PipelineStage) -> None:
        self.stage = new_stage
        self.updated_at = datetime.now(UTC)

    def send_a2a(self, message: A2AMessage) -> None:
        """Record an A2A message in the pipeline context."""
        self.a2a_messages.append(message)

    def get_messages_for(self, agent_name: str) -> list[A2AMessage]:
        """Get all A2A messages addressed to a specific agent."""
        return [m for m in self.a2a_messages if m.to_agent == agent_name]

    def get_messages_from(self, agent_name: str) -> list[A2AMessage]:
        """Get all A2A messages sent by a specific agent."""
        return [m for m in self.a2a_messages if m.from_agent == agent_name]
