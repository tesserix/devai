"""LangGraph state definition for the DevAI ALM pipeline.

The ALMState TypedDict is the single source of truth flowing through the
LangGraph StateGraph. Each node (agent) reads from and writes to this state.
"""

from __future__ import annotations

from typing import Any, TypedDict


class A2AMessageDict(TypedDict, total=False):
    """Serialized A2A message for graph state."""

    id: str
    from_agent: str
    to_agent: str
    message_type: str
    subject: str
    body: str
    payload: dict[str, Any]
    in_reply_to: str | None
    timestamp: str


class StoryBranchDict(TypedDict, total=False):
    """Per-story branch, PR, and quality gate tracking.

    Each story gets its own feature branch and PR. The orchestrator
    manages the lifecycle of each story through implement → review →
    security → test, storing results here.
    """

    story_index: int  # Index into ALMState["stories"]
    story_number: int  # GitHub issue number
    story_title: str
    branch_name: str | None
    pr_number: int | None
    status: str  # "pending" | "implementing" | "in_review" | "approved" | "merged" | "failed"
    review_decision: str  # "approved" | "changes_requested"
    review_iteration: int
    review_feedback: list[str]
    security_decision: str  # "pass" | "pass_with_warnings" | "block"
    test_failed: int
    test_summary: str
    implementation_summary: str
    story_plan: str  # Per-story technical plan from EM


class ALMState(TypedDict, total=False):
    """Full ALM pipeline state flowing through the LangGraph."""

    # --- Pipeline Identity ---
    run_id: str
    repo_full_name: str
    trigger_type: str
    trigger_ref: str
    requirements: str
    stage: str

    # --- Requirements Analysis ---
    analyzed_requirements: list[dict[str, Any]]
    requirement_gaps: list[str]
    stakeholder_questions: list[str]

    # --- Epic & Stories ---
    epics: list[dict[str, Any]]
    stories: list[dict[str, Any]]
    story_issue_numbers: list[int]
    epic_issue_number: int | None

    # --- Story-Level Tracking (collaborative parallel model) ---
    story_branches: list[StoryBranchDict]  # Per-story branch/PR/status
    active_story_index: int  # Index of story currently being processed
    story_plans: list[str]  # Per-story technical plans from EM

    # --- Technical Planning ---
    technical_plan: str
    affected_files: list[str]
    subtasks: list[str]
    plan_complexity: str

    # --- Implementation (active story context — set by story_loop) ---
    branch_name: str | None
    pr_number: int | None
    implementation_summary: str
    committed_files: list[str]

    # --- Code Review (active story context) ---
    review_decision: str  # "approved" | "changes_requested"
    review_summary: str
    review_comments: list[dict[str, str]]
    security_issues: list[str]
    performance_issues: list[str]
    review_iteration: int

    # --- Build / CI ---
    build_run_id: int | None
    build_status: str
    build_url: str
    build_logs: str
    failed_jobs: list[dict[str, str]]

    # --- Testing ---
    test_total: int
    test_passed: int
    test_failed: int
    test_summary: str
    test_failures: list[dict[str, str]]

    # --- Database Engineering ---
    db_decision: str  # "safe" | "review_needed" | "blocked" | "not_applicable"
    db_summary: str

    # --- Security Scan ---
    security_decision: str  # "pass" | "pass_with_warnings" | "block"
    security_summary: str
    security_findings: list[dict[str, Any]]

    # --- Deployment ---
    deploy_status: str
    deploy_environment: str
    deploy_version: str
    deploy_argocd_app: str
    health_check_passed: bool

    # --- Supervisor & Orchestrator ---
    supervisor_plan: dict[str, Any]  # Structured plan from Supervisor Agent
    supervisor_plan_raw: str  # Raw LLM output from Supervisor
    supervisor_tracking_issue: int | None  # SCM tracking issue number
    supervisor_project_id: str | None  # GitHub Project v2 node ID
    supervisor_project_url: str | None  # Project board URL
    orchestrator_routing: dict[str, Any]  # Current routing decision from Orchestrator
    orchestrator_decision_raw: str  # Raw LLM output from Orchestrator
    detected_tech_stack: str  # Tech stack detected by TechDetector

    # --- A2A Communication ---
    a2a_messages: list[A2AMessageDict]

    # --- Governance ---
    governance: str  # CLAUDE.md content

    # --- Agent Memory ---
    memory_context: str  # Injected memory from past runs

    # --- Pipeline Metadata ---
    review_feedback: list[str]
    error: str | None
    agent_timings: dict[str, float]

    # --- Progress Callback ---
    on_progress: Any  # Optional callback: (step, status, detail) -> None
