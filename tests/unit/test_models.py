"""Tests for shared data models."""

from devai.models import (
    AgentResult,
    AgentRole,
    CodeReview,
    PipelineContext,
    PipelineStage,
    ReviewDecision,
    TechnicalPlan,
    TriggerType,
    UserStory,
)


def test_pipeline_context_creation():
    ctx = PipelineContext(
        repo_full_name="tesserix/test-app",
        trigger_type=TriggerType.CLI,
        trigger_ref="cli",
        requirements="Build a health check endpoint",
    )
    assert ctx.run_id  # Auto-generated ULID
    assert ctx.stage == PipelineStage.TRIGGERED
    assert ctx.repo_full_name == "tesserix/test-app"
    assert ctx.artifacts == {}
    assert ctx.review_iteration == 0


def test_pipeline_context_advance_stage():
    ctx = PipelineContext(
        repo_full_name="tesserix/test-app",
        trigger_type=TriggerType.GITHUB_ISSUE,
        trigger_ref="42",
        requirements="Test",
    )
    original_updated = ctx.updated_at
    ctx.advance_stage(PipelineStage.STORIES_CREATED)
    assert ctx.stage == PipelineStage.STORIES_CREATED
    assert ctx.updated_at >= original_updated


def test_pipeline_context_serialization():
    ctx = PipelineContext(
        repo_full_name="tesserix/test-app",
        trigger_type=TriggerType.CLI,
        trigger_ref="cli",
        requirements="Test requirements",
    )
    json_str = ctx.model_dump_json()
    restored = PipelineContext.model_validate_json(json_str)
    assert restored.run_id == ctx.run_id
    assert restored.repo_full_name == ctx.repo_full_name
    assert restored.stage == ctx.stage


def test_agent_result():
    result = AgentResult(
        agent_name="product_director",
        status="success",
        output={"stories_count": 3},
        summary="Created 3 user stories",
        duration_seconds=12.5,
    )
    assert result.agent_name == "product_director"
    assert result.output["stories_count"] == 3


def test_user_story():
    story = UserStory(
        title="Add health check endpoint",
        description="As a DevOps engineer, I want a /healthz endpoint",
        acceptance_criteria=["Returns 200 OK", "Checks DB connectivity"],
        priority="high",
        labels=["feature", "backend"],
    )
    assert len(story.acceptance_criteria) == 2
    assert story.priority == "high"


def test_code_review():
    review = CodeReview(
        decision=ReviewDecision.APPROVED,
        summary="Code looks good",
        security_issues=[],
        performance_issues=[],
    )
    assert review.decision == ReviewDecision.APPROVED


def test_technical_plan():
    plan = TechnicalPlan(
        summary="Add health check endpoint",
        affected_files=["main.go", "routes.go"],
        approach="Add a new handler",
        subtasks=["Create handler", "Add route", "Write tests"],
    )
    assert len(plan.affected_files) == 2
    assert len(plan.subtasks) == 3
