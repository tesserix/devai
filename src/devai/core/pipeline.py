"""Pipeline orchestrator — manages stage transitions and routing."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from devai.models import (
    PipelineContext,
    PipelineStage,
    ReviewDecision,
    TriggerType,
)

if TYPE_CHECKING:
    from devai.config import Settings
    from devai.core.event_bus import EventBus
    from devai.core.state import StateManager

logger = logging.getLogger(__name__)

# Maps each stage to the NATS subject for the next agent
STAGE_ROUTING: dict[PipelineStage, str] = {
    PipelineStage.TRIGGERED: "devai.pipeline.trigger",
    PipelineStage.STORIES_CREATED: "devai.pipeline.stories_ready",
    PipelineStage.PLAN_CREATED: "devai.pipeline.plan_ready",
    PipelineStage.CODE_IMPLEMENTED: "devai.pipeline.code_ready",
    PipelineStage.CODE_REVIEWED: "devai.pipeline.review_complete",
    PipelineStage.TESTS_COMPLETE: "devai.pipeline.tests_complete",
}


class PipelineOrchestrator:
    """Creates and routes pipeline runs."""

    def __init__(self, event_bus: EventBus, state: StateManager, config: Settings) -> None:
        self.event_bus = event_bus
        self.state = state
        self.config = config

    async def trigger(
        self,
        repo_full_name: str,
        trigger_type: TriggerType,
        trigger_ref: str,
        requirements: str,
    ) -> PipelineContext:
        """Create a new pipeline run and publish the trigger event."""
        ctx = PipelineContext(
            repo_full_name=repo_full_name,
            trigger_type=trigger_type,
            trigger_ref=trigger_ref,
            requirements=requirements,
        )

        await self.state.create_run(ctx)
        await self.event_bus.publish("devai.pipeline.trigger", ctx.model_dump_json())
        logger.info("Pipeline triggered: run_id=%s repo=%s", ctx.run_id, repo_full_name)
        return ctx

    async def advance(self, ctx: PipelineContext, new_stage: PipelineStage) -> None:
        """Advance the pipeline to the next stage and publish the event."""
        ctx.advance_stage(new_stage)
        await self.state.update_run_stage(ctx.run_id, new_stage.value)

        subject = STAGE_ROUTING.get(new_stage)
        if subject:
            await self.event_bus.publish(subject, ctx.model_dump_json())
            logger.info("Pipeline advanced: run_id=%s stage=%s", ctx.run_id, new_stage.value)
        else:
            logger.info("Pipeline reached terminal stage: run_id=%s stage=%s", ctx.run_id, new_stage.value)

    async def handle_review_decision(
        self,
        ctx: PipelineContext,
        decision: ReviewDecision,
        feedback: str = "",
    ) -> None:
        """Route based on review decision — approved goes to QA, changes go back to Sr Dev."""
        if decision == ReviewDecision.APPROVED:
            await self.advance(ctx, PipelineStage.CODE_REVIEWED)
        else:
            ctx.review_iteration += 1
            if ctx.review_iteration >= self.config.max_review_iterations:
                logger.warning(
                    "Max review iterations reached: run_id=%s iterations=%d",
                    ctx.run_id,
                    ctx.review_iteration,
                )
                ctx.advance_stage(PipelineStage.FAILED)
                await self.state.update_run_stage(ctx.run_id, PipelineStage.FAILED.value)
                await self.event_bus.publish("devai.pipeline.errors", ctx.model_dump_json())
                return

            # Append review feedback and send back to Sr Developer
            ctx.artifacts.setdefault("review_feedback", []).append(feedback)
            ctx.advance_stage(PipelineStage.REVIEW_CHANGES_REQUESTED)
            await self.state.update_run_stage(ctx.run_id, PipelineStage.REVIEW_CHANGES_REQUESTED.value)
            await self.event_bus.publish("devai.pipeline.plan_ready", ctx.model_dump_json())
            logger.info(
                "Review changes requested: run_id=%s iteration=%d",
                ctx.run_id,
                ctx.review_iteration,
            )

    async def fail(self, ctx: PipelineContext, error: str) -> None:
        """Mark a pipeline run as failed."""
        ctx.advance_stage(PipelineStage.FAILED)
        ctx.artifacts["error"] = error
        await self.state.update_run_stage(ctx.run_id, PipelineStage.FAILED.value)
        await self.event_bus.publish("devai.pipeline.errors", ctx.model_dump_json())
        logger.error("Pipeline failed: run_id=%s error=%s", ctx.run_id, error)
