"""QA Tester Agent — writes and runs Playwright E2E tests using Claude."""

from __future__ import annotations

import logging
from typing import Any

from devai.core.base_agent import BaseAgent
from devai.models import AgentResult, PipelineContext, PipelineStage, TestResult
from devai.providers.anthropic_claude import ClaudeProvider
from devai.tools.github_tools import GITHUB_TOOLS, GitHubToolExecutor
from devai.tools.test_tools import TEST_TOOLS, TestToolExecutor

logger = logging.getLogger(__name__)

# QA gets read tools + commit (for test files) + test execution
QA_TOOLS = [
    t for t in GITHUB_TOOLS
    if t["name"] in {
        "github_get_file_content",
        "github_list_files",
        "github_get_repo_tree",
        "github_get_pr_diff",
        "github_commit_file",
        "github_add_comment",
    }
] + TEST_TOOLS

SYSTEM_PROMPT = """You are a Senior QA Engineer responsible for writing and running E2E tests.

Your responsibilities:
1. Analyze the PR changes to understand what was implemented
2. Read existing test files to understand testing patterns
3. Write comprehensive Playwright E2E tests
4. Commit the test files to the branch
5. Run the tests and report results

Testing guidelines:
- Use Playwright with TypeScript or JavaScript (match the project's language)
- Follow existing test patterns in the repository
- Test happy paths AND error/edge cases
- Use descriptive test names that explain the expected behavior
- Use page object patterns if the project already uses them
- Include proper selectors (prefer data-testid, aria-label, or role-based)
- Handle async operations with proper waits
- Keep tests independent and idempotent

Process:
1. Use github_get_pr_diff to understand what changed
2. Explore existing test files with github_list_files and github_get_file_content
3. Write test files and commit them with github_commit_file
4. Run tests with run_playwright_test
5. Post results as a comment on the PR with github_add_comment

Important:
- Test files should go in the project's test directory (e.g., tests/, e2e/, __tests__/)
- Include proper setup and teardown
- Do NOT test implementation details, test user-facing behavior"""


class QATesterAgent(BaseAgent):
    """Writes and runs Playwright E2E tests using Claude with tools."""

    name = "qa_tester"
    subscribe_subject = "devai.pipeline.review_complete"
    publish_subject = "devai.pipeline.tests_complete"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.claude = ClaudeProvider(self.config)
        self.github_tools = GitHubToolExecutor(self.github)
        self.test_tools = TestToolExecutor()

    async def _tool_executor(self, tool_name: str, tool_input: dict[str, Any]) -> str:
        """Route tool calls to the appropriate executor."""
        if tool_name.startswith("github_"):
            return await self.github_tools.execute(tool_name, tool_input)
        return await self.test_tools.execute(tool_name, tool_input)

    async def execute(self, ctx: PipelineContext) -> AgentResult:
        """Write and run E2E tests for the implemented changes."""
        pr_number = ctx.pr_number
        branch = ctx.branch_name
        repo = ctx.repo_full_name

        if not pr_number or not branch:
            return AgentResult(
                agent_name=self.name,
                status="failed",
                summary="No PR or branch found in pipeline context",
            )

        stories = ctx.artifacts.get("product_director", {}).get("issues", [])
        issue_refs = "\n".join(f"- #{s['number']}: {s['title']}" for s in stories)

        user_message = f"""Repository: {repo}
PR: #{pr_number}
Branch: {branch}

## User Stories
{issue_refs}

## Original Requirements
{ctx.requirements}

Write comprehensive E2E tests for the changes in this PR. Then run them and report the results.

Start by reading the PR diff to understand what was changed, explore existing tests for patterns, then write and commit test files, and finally run them."""

        # Inject CLAUDE.md governance rules into system prompt
        system = SYSTEM_PROMPT
        governance = ctx.artifacts.get("governance", "")
        if governance:
            system += f"\n\n## Repository Governance (CLAUDE.md)\nYou MUST follow these rules:\n\n{governance}"

        result_text = await self.claude.run_agent_loop(
            system_prompt=system,
            user_message=user_message,
            tools=QA_TOOLS,
            tool_executor=self._tool_executor,
        )

        # Post final results as a PR comment
        if pr_number:
            await self.github.add_comment(
                repo=repo,
                issue_number=pr_number,
                body=f"## QA Test Results\n\n{result_text}",
            )

        ctx.advance_stage(PipelineStage.TESTS_COMPLETE)

        return AgentResult(
            agent_name=self.name,
            status="success",
            output={"test_summary": result_text},
            summary=f"E2E tests completed for PR #{pr_number}",
        )
