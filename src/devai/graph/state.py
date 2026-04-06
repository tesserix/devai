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

    # --- Technical Planning ---
    technical_plan: str
    affected_files: list[str]
    subtasks: list[str]
    plan_complexity: str

    # --- Implementation ---
    branch_name: str | None
    pr_number: int | None
    implementation_summary: str
    committed_files: list[str]

    # --- Code Review ---
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
