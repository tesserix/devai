"""Senior Developer Agent — implements code based on technical plans using Claude."""

from __future__ import annotations

import logging
from typing import Any

from devai.core.base_agent import BaseAgent
from devai.graph.a2a import A2ABus
from devai.graph.state import ALMState
from devai.providers.anthropic_claude import ClaudeProvider
from devai.tools.github_tools import GITHUB_TOOLS, GitHubToolExecutor
from devai.tools.validation_tools import VALIDATION_TOOLS, ValidationToolExecutor

logger = logging.getLogger(__name__)

# Sr Dev gets full read/write tools + validation tools
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
] + VALIDATION_TOOLS

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
- Ensure all file paths are correct relative to the repo root

## Mandatory Guardrails (MUST follow before creating PR)

After implementing code, you MUST run these validation checks:
1. `validate_compile` — ensure the code compiles/type-checks with zero errors
2. `validate_lint` — ensure the code passes the project linter with zero errors
3. `validate_unit_tests` — ensure all unit tests pass
4. `validate_format` — ensure code formatting matches project standards

If ANY validation fails:
- Fix the issue immediately
- Re-commit the fix
- Re-run the failing validation
- Only create the PR after ALL validations pass

Do NOT skip validations. Do NOT create a PR with failing checks."""


class SeniorDeveloperAgent(BaseAgent):
    """Implements code based on technical plans using Claude with GitHub tools."""

    name = "senior_developer"
    subscribe_subject = "devai.pipeline.plan_ready"
    publish_subject = "devai.pipeline.code_ready"

    async def _execute_graph(self, state: ALMState, a2a: A2ABus) -> dict[str, Any]:
        """Implement code based on the technical plan."""
        claude = ClaudeProvider(self.config)
        github_tools = GitHubToolExecutor(self.github)
        validation_tools = ValidationToolExecutor()

        async def tool_executor(tool_name: str, tool_input: dict[str, Any]) -> str:
            if tool_name.startswith("github_"):
                return await github_tools.execute(tool_name, tool_input)
            if tool_name.startswith("validate_"):
                return await validation_tools.execute(tool_name, tool_input)
            return f"Unknown tool: {tool_name}"

        repo = state.get("repo_full_name", "")
        plan = state.get("technical_plan", "")
        stories = state.get("stories", [])
        requirements = state.get("requirements", "")

        issue_refs = "\n".join(f"- #{s.get('number', '?')}: {s.get('title', '')}" for s in stories)

        # Check for A2A messages (escalations from CI, review feedback, etc.)
        inbox_context = a2a.format_inbox_context()

        # Check for escalations from CI Monitor
        escalations = a2a.get_escalations()
        ci_fix_context = ""
        if escalations:
            ci_issues = "\n".join(f"- {e['subject']}: {e['body'][:200]}" for e in escalations)
            ci_fix_context = f"\n\n## CI Issues to Fix\n{ci_issues}"

        user_message = f"""Repository: {repo}
Run ID: {state.get('run_id', '')}

## Technical Plan
{plan}

## Related Issues
{issue_refs}

## Original Requirements
{requirements[:2000]}
{ci_fix_context}
{inbox_context}

Implement the code changes described in the technical plan. Create a feature branch, commit all changes, and open a pull request."""

        # If revision iteration, include review feedback
        review_feedback = state.get("review_feedback", [])
        if review_feedback:
            feedback_text = "\n\n".join(review_feedback)
            branch = state.get("branch_name", "")
            user_message += f"""

## Review Feedback (Iteration {state.get('review_iteration', 0)})
The Staff Reviewer requested the following changes. Address ALL of them:
{feedback_text}

Use the existing branch if one exists (branch: {branch}), or create a new one."""

        system = SYSTEM_PROMPT
        governance = state.get("governance", "")
        if governance:
            system += f"\n\n## Repository Governance (CLAUDE.md)\nYou MUST follow these rules:\n\n{governance}"

        result_text = await claude.run_agent_loop(
            system_prompt=system,
            user_message=user_message,
            tools=SR_DEV_TOOLS,
            tool_executor=tool_executor,
        )

        # Notify reviewer that code is ready
        a2a.handoff(
            "staff_reviewer",
            "Code Ready for Review",
            f"Implementation complete. PR created on repo {repo}.\n\n{result_text[:300]}...",
        )

        # Notify CI Monitor to watch the build
        a2a.notify(
            "ci_monitor",
            "Code Pushed",
            f"New code pushed to branch for PR review. Watch for CI builds.",
        )

        return {
            "implementation_summary": result_text,
            "branch_name": state.get("branch_name"),
            "pr_number": state.get("pr_number"),
        }
