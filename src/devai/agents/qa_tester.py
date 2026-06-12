"""QA Tester Agent — writes and runs Playwright E2E tests using Claude."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from devai.agents.skills import get_skill_profile
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
    t
    for t in GITHUB_TOOLS
    if t["name"]
    in {
        "github_get_file_content",
        "github_list_files",
        "github_get_repo_tree",
        "github_get_pr_diff",
        "github_commit_file",
        "github_add_comment",
        "github_ci_status",
        "github_ci_failure_logs",
        "github_rerun_failed_jobs",
    }
] + TEST_TOOLS

SYSTEM_PROMPT = """You are a Senior QA Engineer responsible for writing and running E2E tests.

## Lane Boundaries — STRICT

You ONLY add or modify TEST files. You NEVER edit application source code, even if you
think there's a bug. If you find a bug while writing tests, file it as a comment on the PR
and let the Senior Developer fix it on the next iteration.

You ONLY touch:
- `tests/`, `test/`, `__tests__/`, `e2e/`, `spec/` directories
- Files matching `*.test.{ts,tsx,js,jsx}`, `*.spec.{ts,tsx,js,jsx}`, `*_test.go`,
  `test_*.py`, depending on the stack
- Test infrastructure setup files (`vitest.setup.ts`, `jest.config.js`, `conftest.py`)
  ONLY if they don't exist yet for the test framework you need
- The PR description / comments

You NEVER touch:
- Any application source file under `src/`, `app/`, `lib/`, `internal/`, `cmd/`, etc.
- `.github/workflows/` (CI Engineer's lane)
- `Dockerfile`, `helm/`, `migrations/`
- `package.json` or other manifests, except to add a dev-only test dependency

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
        claude = ClaudeProvider(self.config, model=getattr(self.config, "llm_model_review", None))
        github_tools = GitHubToolExecutor(self.github)
        test_tools = TestToolExecutor(self.github)
        latest_test_summary: dict[str, Any] = {}

        async def tool_executor(tool_name: str, tool_input: dict[str, Any]) -> str:
            if tool_name.startswith("github_"):
                return await github_tools.execute(tool_name, tool_input)
            result = await test_tools.execute(tool_name, tool_input)
            nonlocal latest_test_summary
            if tool_name == "run_playwright_test":
                latest_test_summary = self._extract_test_summary(result)
            elif tool_name == "parse_test_results":
                latest_test_summary = self._extract_json_object(result)
            return result

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

        # Get active story context
        active_idx = state.get("active_story_index", 0)
        stories = state.get("stories", [])
        active_story = stories[active_idx] if active_idx < len(stories) else {}
        story_number = active_story.get("number", "?")
        story_title = active_story.get("title", "")
        acceptance_criteria = active_story.get("acceptance_criteria", [])
        memory_context = state.get("memory_context", "")

        ac_text = "\n".join(f"- {c}" for c in acceptance_criteria) if acceptance_criteria else "(none)"

        # Check for A2A messages
        inbox_context = a2a.format_inbox_context()

        user_message = f"""Repository: {repo}
PR: #{pr_number}
Branch: {branch}

## Story Under Test
Story #{story_number}: {story_title}
Description: {active_story.get("description", "")[:500]}

### Acceptance Criteria to Verify
{ac_text}

## Original Requirements
{state.get("requirements", "")[:1500]}

{inbox_context}

## Relevant Memory From Past Runs
{memory_context or "(none)"}

Write E2E tests that verify the acceptance criteria for Story #{story_number}. Then run them and report results.

Start by reading the PR diff to understand what was changed, explore existing tests for patterns, then write and commit test files, and finally run them."""

        system = SYSTEM_PROMPT
        profile = get_skill_profile(state.get("skill_profile_name"))
        system += "\n\n" + profile.render_for_qa()
        logger.info("QA Tester running with skill profile: %s", profile.display_name)

        governance = state.get("governance", "")
        if governance:
            system += f"\n\n## Repository Governance (CLAUDE.md)\nYou MUST follow these rules:\n\n{governance}"

        result_text = await claude.run_agent_loop(
            system_prompt=system,
            user_message=user_message,
            tools=QA_TOOLS,
            tool_executor=tool_executor,
            max_iterations=self.config.claude_max_iterations_ops,
        )

        # Post results as a PR comment — best-effort: the tests already ran;
        # a failed comment must never fail the stage (one bad kwarg here
        # killed an otherwise-perfect 15-stage run). SCMClient.add_comment
        # takes (repo, issue_id, body) — the old issue_number kwarg only
        # existed on the legacy core client.
        if pr_number:
            heading = f"## QA Test Results — Story #{story_number}" if story_number else "## QA Test Results"
            try:
                await self.github.add_comment(repo, pr_number, f"{heading}\n\n{result_text}")
            except Exception:  # noqa: BLE001
                logger.exception("QA results comment failed for PR #%s", pr_number)

        # Notify about test completion for this story
        a2a.notify(
            "orchestrator",
            f"Story #{story_number} Tests Complete",
            f"E2E tests completed for PR #{pr_number} (Story #{story_number}).\n\n{result_text[:300]}...",
        )

        # Notify CI Monitor
        a2a.notify(
            "ci_monitor",
            f"Story #{story_number} Test Suite Committed",
            f"Test files committed to branch '{branch}' for Story #{story_number}.",
        )

        return {
            "test_summary": result_text,
            "test_total": int(latest_test_summary.get("total", 0)),
            "test_passed": int(latest_test_summary.get("passed", 0)),
            "test_failed": int(latest_test_summary.get("failed", 0)),
            "test_failures": latest_test_summary.get("failures", []),
        }

    @staticmethod
    def _extract_json_object(text: str) -> dict[str, Any]:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {}

    @classmethod
    def _extract_test_summary(cls, text: str) -> dict[str, Any]:
        data = cls._extract_json_object(text)
        if "summary" in data and isinstance(data["summary"], dict):
            return data["summary"]
        if {"total", "passed", "failed"} <= data.keys():
            return data
        return {}
