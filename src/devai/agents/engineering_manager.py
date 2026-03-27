"""Engineering Manager Agent — analyzes user stories and creates technical plans using Claude."""

from __future__ import annotations

import json
import logging
from typing import Any

from devai.core.base_agent import BaseAgent
from devai.models import AgentResult, PipelineContext, PipelineStage, TechnicalPlan
from devai.providers.anthropic_claude import ClaudeProvider
from devai.tools.github_tools import GITHUB_TOOLS, GitHubToolExecutor

logger = logging.getLogger(__name__)

# EM gets read-only + comment tools
EM_TOOLS = [
    t for t in GITHUB_TOOLS
    if t["name"] in {
        "github_get_issue",
        "github_get_file_content",
        "github_list_files",
        "github_get_repo_tree",
        "github_add_comment",
    }
]

SYSTEM_PROMPT = """You are a Senior Engineering Manager planning the technical implementation of user stories.

Your responsibilities:
1. Analyze the user stories and repository structure
2. Understand the existing codebase, patterns, and conventions
3. Create a detailed technical plan for implementation

Process:
1. First, use github_get_repo_tree to understand the repo structure
2. Read key files to understand patterns, frameworks, and conventions
3. Read the user story issues for full context
4. Create a comprehensive technical plan

Your technical plan must include:
- Summary of what needs to be built
- List of files that need to be created or modified
- Detailed approach for implementation
- Subtasks broken down into logical units
- Dependencies between subtasks
- Estimated complexity (low/medium/high)

Consider:
- Existing code patterns and conventions in the repo
- Test requirements
- Error handling patterns
- API design consistency
- Database schema changes if needed

Output the plan as a structured document. Be specific about file paths, function names, and approach."""


class EngineeringManagerAgent(BaseAgent):
    """Analyzes requirements and creates technical implementation plans using Claude."""

    name = "engineering_manager"
    subscribe_subject = "devai.pipeline.stories_ready"
    publish_subject = "devai.pipeline.plan_ready"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.claude = ClaudeProvider(self.config)
        self.tool_executor = GitHubToolExecutor(self.github)

    async def execute(self, ctx: PipelineContext) -> AgentResult:
        """Analyze stories and create a technical plan."""
        # Build context from previous agent's output
        stories_info = ctx.artifacts.get("product_director", {})
        issues = stories_info.get("issues", [])
        issue_refs = "\n".join(
            f"- #{i['number']}: {i['title']} (priority: {i['priority']})"
            for i in issues
        )

        user_message = f"""Repository: {ctx.repo_full_name}

## User Stories to Implement
{issue_refs}

## Original Requirements
{ctx.requirements}

Please analyze the repository structure and these user stories, then create a detailed technical implementation plan.

Start by exploring the repo structure, then read relevant files to understand the codebase patterns."""

        # If this is a revision after review feedback, include it
        review_feedback = ctx.artifacts.get("review_feedback", [])
        if review_feedback:
            feedback_text = "\n\n".join(review_feedback)
            user_message += f"""

## Review Feedback from Previous Iteration
The Staff Reviewer requested changes. Please incorporate this feedback into your revised plan:
{feedback_text}"""

        # Inject CLAUDE.md governance rules into system prompt
        system = SYSTEM_PROMPT
        governance = ctx.artifacts.get("governance", "")
        if governance:
            system += f"\n\n## Repository Governance (CLAUDE.md)\nYou MUST follow these rules:\n\n{governance}"

        plan_text = await self.claude.run_agent_loop(
            system_prompt=system,
            user_message=user_message,
            tools=EM_TOOLS,
            tool_executor=self.tool_executor.execute,
        )

        # Post the plan as a comment on the first issue
        if issues:
            first_issue = issues[0]["number"]
            plan_comment = f"## Technical Implementation Plan\n\n{plan_text}"
            await self.github.add_comment(ctx.repo_full_name, first_issue, plan_comment)

        ctx.advance_stage(PipelineStage.PLAN_CREATED)

        return AgentResult(
            agent_name=self.name,
            status="success",
            output={
                "plan": plan_text,
                "issues_analyzed": len(issues),
            },
            summary=f"Technical plan created for {len(issues)} user stories",
        )
