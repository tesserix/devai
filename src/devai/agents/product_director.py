"""Product Director Agent — creates user stories from high-level requirements using OpenAI Codex."""

from __future__ import annotations

import json
import logging
from typing import Any

from devai.core.base_agent import BaseAgent
from devai.models import AgentResult, PipelineContext, PipelineStage, UserStory
from devai.providers.openai_codex import CodexLiteProvider

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a Senior Product Director at a world-class software company.

Your role is to take high-level product requirements and break them down into well-structured user stories.

For each user story, provide:
- A clear, concise title
- A detailed description with context
- Specific acceptance criteria (testable conditions)
- Priority (low, medium, high, critical)
- Relevant labels for categorization

Output ONLY valid JSON — an array of user story objects:
[
  {
    "title": "User story title",
    "description": "As a [user type], I want [goal] so that [benefit].\n\nContext: ...",
    "acceptance_criteria": ["Given X, when Y, then Z", ...],
    "priority": "high",
    "labels": ["feature", "frontend"]
  }
]

Guidelines:
- Each story should be independently deliverable
- Stories should be small enough for a single developer to implement in 1-3 days
- Acceptance criteria must be specific and testable
- Include edge cases and error scenarios where relevant
- Use the "As a [user], I want [goal], so that [benefit]" format for descriptions
- Prioritize based on user impact and dependencies"""


class ProductDirectorAgent(BaseAgent):
    """Creates user stories from requirements using OpenAI Codex Responses API."""

    name = "product_director"
    subscribe_subject = "devai.pipeline.trigger"
    publish_subject = "devai.pipeline.stories_ready"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.codex = CodexLiteProvider(self.config)

    async def execute(self, ctx: PipelineContext) -> AgentResult:
        """Generate user stories from requirements."""
        prompt = f"""Repository: {ctx.repo_full_name}
Trigger: {ctx.trigger_type.value} (ref: {ctx.trigger_ref})

Requirements:
{ctx.requirements}

Break these requirements into user stories. Consider the repository context and ensure stories are actionable for developers."""

        response = await self.codex.generate(
            prompt=prompt,
            system=SYSTEM_PROMPT,
            response_format={"type": "json_object"},
        )

        # Parse the stories
        try:
            stories_data = json.loads(response)
            if isinstance(stories_data, dict) and "stories" in stories_data:
                stories_data = stories_data["stories"]
            stories = [UserStory.model_validate(s) for s in stories_data]
        except (json.JSONDecodeError, ValueError) as e:
            logger.error("Failed to parse stories from Codex response: %s", e)
            return AgentResult(
                agent_name=self.name,
                status="failed",
                summary=f"Failed to parse user stories: {e}",
            )

        # Create GitHub issues for each story
        created_issues: list[dict[str, Any]] = []
        for story in stories:
            body = f"{story.description}\n\n## Acceptance Criteria\n"
            for i, ac in enumerate(story.acceptance_criteria, 1):
                body += f"\n- [ ] {ac}"
            body += f"\n\n**Priority:** {story.priority}"

            labels = [*story.labels, "devai:user-story", f"priority:{story.priority}"]

            issue = await self.github.create_issue(
                repo=ctx.repo_full_name,
                title=story.title,
                body=body,
                labels=labels,
            )
            created_issues.append({
                "number": issue["number"],
                "title": story.title,
                "priority": story.priority,
                "url": issue["html_url"],
            })

        ctx.advance_stage(PipelineStage.STORIES_CREATED)

        return AgentResult(
            agent_name=self.name,
            status="success",
            output={
                "stories_count": len(created_issues),
                "issues": created_issues,
            },
            summary=f"Created {len(created_issues)} user stories as GitHub issues",
        )
