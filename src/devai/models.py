"""Shared data models for the DevAI pipeline."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field
from ulid import ULID


class PipelineStage(str, Enum):
    TRIGGERED = "triggered"
    STORIES_CREATED = "stories_created"
    PLAN_CREATED = "plan_created"
    CODE_IMPLEMENTED = "code_implemented"
    CODE_REVIEWED = "code_reviewed"
    REVIEW_CHANGES_REQUESTED = "review_changes_requested"
    TESTS_COMPLETE = "tests_complete"
    DONE = "done"
    FAILED = "failed"


class TriggerType(str, Enum):
    GITHUB_ISSUE = "github_issue"
    PROJECT_CARD = "project_card"
    CLI = "cli"


class ReviewDecision(str, Enum):
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"


class AgentRole(str, Enum):
    PRODUCT_DIRECTOR = "product_director"
    ENGINEERING_MANAGER = "engineering_manager"
    SENIOR_DEVELOPER = "senior_developer"
    STAFF_REVIEWER = "staff_reviewer"
    QA_TESTER = "qa_tester"


class AgentResult(BaseModel):
    agent_name: str
    status: str  # "success" | "needs_retry" | "failed"
    output: dict[str, Any] = Field(default_factory=dict)
    summary: str = ""
    duration_seconds: float = 0.0


class UserStory(BaseModel):
    title: str
    description: str
    acceptance_criteria: list[str] = Field(default_factory=list)
    priority: str = "medium"  # low | medium | high | critical
    labels: list[str] = Field(default_factory=list)


class TechnicalPlan(BaseModel):
    summary: str
    affected_files: list[str] = Field(default_factory=list)
    approach: str = ""
    subtasks: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    estimated_complexity: str = "medium"  # low | medium | high


class CodeReview(BaseModel):
    decision: ReviewDecision
    summary: str
    comments: list[dict[str, str]] = Field(default_factory=list)  # [{file, line, body}]
    security_issues: list[str] = Field(default_factory=list)
    performance_issues: list[str] = Field(default_factory=list)
    style_issues: list[str] = Field(default_factory=list)


class TestResult(BaseModel):
    total: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    test_files: list[str] = Field(default_factory=list)
    failures: list[dict[str, str]] = Field(default_factory=list)  # [{test_name, error}]
    screenshots: list[str] = Field(default_factory=list)


class PipelineContext(BaseModel):
    run_id: str = Field(default_factory=lambda: str(ULID()))
    repo_full_name: str
    trigger_type: TriggerType
    trigger_ref: str  # Issue number, card ID, or CLI input ref
    requirements: str
    stage: PipelineStage = PipelineStage.TRIGGERED
    branch_name: str | None = None
    pr_number: int | None = None
    review_iteration: int = 0
    artifacts: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def advance_stage(self, new_stage: PipelineStage) -> None:
        self.stage = new_stage
        self.updated_at = datetime.now(timezone.utc)
