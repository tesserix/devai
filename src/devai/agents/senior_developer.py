"""Senior Developer Agent — implements code based on technical plans using Claude."""

from __future__ import annotations

import logging
from typing import Any

from devai.core.base_agent import BaseAgent
from devai.models import AgentResult, PipelineContext, PipelineStage
from devai.providers.anthropic_claude import ClaudeProvider
from devai.tools.github_tools import GITHUB_TOOLS, GitHubToolExecutor

logger = logging.getLogger(__name__)

# Sr Dev gets full read/write tools
SR_DEV_TOOLS = [
    t for t in GITHUB_TOOLS
    if t["name"] in {
        "github_get_file_content",
        "github_list_files",
        "github_get_repo_tree",
        "github_create_branch",
        "github_commit_file",
        "github_create_pull_request",
        "github_add_comment",
    }
]

SYSTEM_PROMPT = """You are a Senior Software Engineer implementing features based on a technical plan.

Your responsibilities:
1. Read and understand the technical plan
2. Explore the existing codebase to understand patterns
3. Create a feature branch
4. Implement the code changes file by file
5. Create a pull request with a clear description

Implementation guidelines:
- Follow existing code patterns and conventions in the repository
- Write clean, well-structured code
- Include appropriate error handling
- Do NOT add unnecessary comments or documentation
- Do NOT include AI-generated disclaimers or co-authored-by lines
- Keep changes focused and minimal — implement exactly what the plan specifies
- Use consistent naming conventions with the existing codebase

Process:
1. Use github_get_repo_tree to understand the full structure
2. Read existing files that you'll modify or that demonstrate patterns
3. Create a feature branch with github_create_branch (name: devai/<short-description>)
4. Implement changes by committing files with github_commit_file
5. Create a pull request with github_create_pull_request

Important:
- Each commit message should describe what changed and why
- The PR description should reference the original issue(s)
- Never commit secrets, API keys, or credentials
- Ensure all file paths are correct relative to the repo root"""


class SeniorDeveloperAgent(BaseAgent):
    """Implements code based on technical plans using Claude with GitHub tools."""

    name = "senior_developer"
    subscribe_subject = "devai.pipeline.plan_ready"
    publish_subject = "devai.pipeline.code_ready"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.claude = ClaudeProvider(self.config)
        self.tool_executor = GitHubToolExecutor(self.github)

    async def execute(self, ctx: PipelineContext) -> AgentResult:
        """Implement code based on the technical plan."""
        plan = ctx.artifacts.get("engineering_manager", {}).get("plan", "")
        stories = ctx.artifacts.get("product_director", {}).get("issues", [])

        issue_refs = "\n".join(f"- #{s['number']}: {s['title']}" for s in stories)

        user_message = f"""Repository: {ctx.repo_full_name}
Run ID: {ctx.run_id}

## Technical Plan
{plan}

## Related Issues
{issue_refs}

## Original Requirements
{ctx.requirements}

Implement the code changes described in the technical plan. Create a feature branch, commit all changes, and open a pull request."""

        # If revision iteration, include review feedback
        review_feedback = ctx.artifacts.get("review_feedback", [])
        if review_feedback:
            feedback_text = "\n\n".join(review_feedback)
            user_message += f"""

## Review Feedback (Iteration {ctx.review_iteration})
The Staff Reviewer requested the following changes. Address ALL of them:
{feedback_text}

Use the existing branch if one exists (branch: {ctx.branch_name}), or create a new one."""

        # Inject CLAUDE.md governance rules into system prompt
        system = SYSTEM_PROMPT
        governance = ctx.artifacts.get("governance", "")
        if governance:
            system += f"\n\n## Repository Governance (CLAUDE.md)\nYou MUST follow these rules:\n\n{governance}"

        result_text = await self.claude.run_agent_loop(
            system_prompt=system,
            user_message=user_message,
            tools=SR_DEV_TOOLS,
            tool_executor=self.tool_executor.execute,
        )

        # Try to extract branch name and PR number from the result
        # The agent should have created these via tools
        branch_name = ctx.branch_name
        pr_number = ctx.pr_number

        # Parse branch/PR info from artifacts if the tools set them
        # (The tool executor returns commit/PR data that Claude sees)

        ctx.advance_stage(PipelineStage.CODE_IMPLEMENTED)
        if branch_name:
            ctx.branch_name = branch_name
        if pr_number:
            ctx.pr_number = pr_number

        return AgentResult(
            agent_name=self.name,
            status="success",
            output={
                "implementation_summary": result_text,
                "branch": branch_name,
                "pr_number": pr_number,
            },
            summary=f"Code implemented on branch {branch_name}, PR #{pr_number}",
        )
