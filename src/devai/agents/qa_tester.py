"""QA Tester Agent — writes and runs Playwright E2E tests using Claude."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from devai.core.base_agent import BaseAgent
from devai.providers.anthropic_claude import ClaudeProvider
from devai.tools.github_tools import GITHUB_TOOLS, GitHubToolExecutor
from devai.tools.test_tools import TEST_TOOLS, TestToolExecutor

if TYPE_CHECKING:
    from devai.graph.a2a import A2ABus
    from devai.graph.state import ALMState

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

    async def _execute_graph(self, state: ALMState, a2a: A2ABus) -> dict[str, Any]:
        """Write and run E2E tests for the implemented changes."""
        claude = ClaudeProvider(self.config)
        github_tools = GitHubToolExecutor(self.github)
        test_tools = TestToolExecutor()

        async def tool_executor(tool_name: str, tool_input: dict[str, Any]) -> str:
            if tool_name.startswith("github_"):
                return await github_tools.execute(tool_name, tool_input)
            return await test_tools.execute(tool_name, tool_input)

        pr_number = state.get("pr_number")
        branch = state.get("branch_name")
        repo = state.get("repo_full_name", "")

        if not pr_number or not branch:
            a2a.escalate(
                "engineering_manager",
                "Testing Blocked",
                "No PR or branch found in pipeline state for testing.",
            )
            return {
                "test_total": 0,
                "test_passed": 0,
                "test_failed": 0,
                "test_summary": "No PR or branch found in pipeline context",
            }

        stories = state.get("stories", [])
        issue_refs = "\n".join(f"- #{s.get('number', '?')}: {s.get('title', '')}" for s in stories)

        # Check for A2A messages
        inbox_context = a2a.format_inbox_context()

        user_message = f"""Repository: {repo}
PR: #{pr_number}
Branch: {branch}

## User Stories
{issue_refs}

## Original Requirements
{state.get('requirements', '')[:2000]}

{inbox_context}

Write comprehensive E2E tests for the changes in this PR. Then run them and report the results.

Start by reading the PR diff to understand what was changed, explore existing tests for patterns, then write and commit test files, and finally run them."""

        system = SYSTEM_PROMPT
        governance = state.get("governance", "")
        if governance:
            system += f"\n\n## Repository Governance (CLAUDE.md)\nYou MUST follow these rules:\n\n{governance}"

        result_text = await claude.run_agent_loop(
            system_prompt=system,
            user_message=user_message,
            tools=QA_TOOLS,
            tool_executor=tool_executor,
        )

        # Post results as a PR comment
        if pr_number:
            await self.github.add_comment(
                repo=repo,
                issue_number=pr_number,
                body=f"## QA Test Results\n\n{result_text}",
            )

        # Notify Release Manager about test completion
        a2a.handoff(
            "release_manager",
            "Tests Complete",
            f"E2E tests completed for PR #{pr_number}.\n\n{result_text[:300]}...",
        )

        # Notify CI Monitor
        a2a.notify(
            "ci_monitor",
            "Test Suite Committed",
            f"Test files committed to branch '{branch}'. Monitor for CI.",
        )

        return {
            "test_summary": result_text,
            "test_total": 0,  # Will be populated from actual test results
            "test_passed": 0,
            "test_failed": 0,
        }
