"""Staff Developer/Reviewer Agent — reviews code using Claude tool-use loop."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from devai.core.base_agent import BaseAgent
from devai.models import CodeReview, ReviewDecision

# Primary: Anthropic Claude | Fallback: OpenAI
from devai.providers.anthropic_claude import ClaudeProvider
from devai.tools.github_tools import GITHUB_TOOLS, GitHubToolExecutor

if TYPE_CHECKING:
    from devai.graph.a2a import A2ABus
    from devai.graph.state import ALMState

logger = logging.getLogger(__name__)


class StaffReviewerAgent(BaseAgent):
    """Reviews code for standards, optimization, and security using Claude tool-use loop."""

    name = "staff_reviewer"
    subscribe_subject = "devai.pipeline.code_ready"
    publish_subject = ""

    # Reviewer gets read-only tools + PR review tools
    REVIEWER_TOOLS = [
        t
        for t in GITHUB_TOOLS
        if t["name"]
        in {
            "github_get_file_content",
            "github_list_files",
            "github_get_repo_tree",
            "github_get_pr_diff",
            "github_add_comment",
            "github_create_pr_review",
        }
    ]

    async def _execute_graph(self, state: ALMState, a2a: A2ABus) -> dict[str, Any]:
        """Review the PR code using Claude tool-use loop."""
        claude = ClaudeProvider(self.config)
        tool_executor = GitHubToolExecutor(self.github)

        pr_number = state.get("pr_number")
        branch = state.get("branch_name")
        repo = state.get("repo_full_name", "")

        if not pr_number or not branch:
            a2a.escalate(
                "engineering_manager",
                "Review Blocked",
                "No PR or branch found in pipeline context for review.",
            )
            return {
                "review_decision": "changes_requested",
                "review_summary": "No PR or branch found in pipeline context",
            }

        # Check for messages from other agents
        inbox_context = a2a.format_inbox_context()

        review_system = """You are a Staff Software Engineer performing a thorough code review.

Focus on:
1. Code quality and adherence to project conventions
2. Performance: unnecessary allocations, N+1 queries, blocking calls
3. Security: injection, auth bypass, secrets in code, OWASP Top 10
4. Error handling: proper error propagation, no swallowed errors
5. Testing: are the changes testable? Any obvious missing test cases?

Be thorough but fair. Only request changes for genuine issues, not style preferences.

After reviewing, output your review as JSON:
{
    "decision": "approved" or "changes_requested",
    "summary": "Overall review summary",
    "comments": ["List of specific comments"],
    "security_issues": ["Any security issues found"],
    "performance_issues": ["Any performance issues found"],
    "style_issues": ["Any style issues found"]
}"""

        review_prompt = f"""Review this pull request:

PR #{pr_number} on {repo}
Branch: {branch}

## Requirements
{state.get("requirements", "")[:2000]}

{inbox_context}

Start by getting the PR diff with github_get_pr_diff, then explore the repo structure and read relevant files to understand the codebase context. Provide a thorough review."""

        governance = state.get("governance", "")
        if governance:
            review_system += f"\n\n## Repository Governance (CLAUDE.md)\nYou MUST follow these rules:\n\n{governance}"

        result_text = await claude.run_agent_loop(
            system_prompt=review_system,
            user_message=review_prompt,
            tools=self.REVIEWER_TOOLS,
            tool_executor=tool_executor.execute,
        )

        review = self._parse_review_text(result_text)

        # Post the review on the PR
        event = "APPROVE" if review.decision == ReviewDecision.APPROVED else "REQUEST_CHANGES"
        review_body = self._format_review_body(review)
        await self.github.create_pr_review(
            repo=repo,
            pr_number=pr_number,
            body=review_body,
            event=event,
        )

        # A2A communication based on review decision
        if review.decision == ReviewDecision.APPROVED:
            a2a.handoff(
                "ci_monitor",
                "Code Approved — Monitor Build",
                f"PR #{pr_number} approved. Monitor the CI build.",
            )
            a2a.notify(
                "qa_tester",
                "Code Approved",
                f"PR #{pr_number} approved by review. Prepare for E2E testing.",
            )
        else:
            a2a.escalate(
                "senior_developer",
                "Changes Requested",
                f"Review of PR #{pr_number} requested changes:\n\n{review.summary}",
                payload={
                    "security_issues": review.security_issues,
                    "performance_issues": review.performance_issues,
                },
            )

        # Update review iteration
        current_iteration = state.get("review_iteration", 0)
        feedback = state.get("review_feedback", [])
        if review.decision == ReviewDecision.CHANGES_REQUESTED:
            feedback = feedback + [review.summary]

        return {
            "review_decision": review.decision.value,
            "review_summary": review.summary,
            "review_comments": review.comments,
            "security_issues": review.security_issues,
            "performance_issues": review.performance_issues,
            "review_iteration": current_iteration + (1 if review.decision == ReviewDecision.CHANGES_REQUESTED else 0),
            "review_feedback": feedback,
        }

    def _parse_review_text(self, result_text: str) -> CodeReview:
        """Parse Claude tool-use loop output into a structured CodeReview."""
        try:
            start = result_text.find("{")
            end = result_text.rfind("}") + 1
            if start >= 0 and end > start:
                data = json.loads(result_text[start:end])
                decision = ReviewDecision(data.get("decision", "changes_requested"))
                return CodeReview(
                    decision=decision,
                    summary=data.get("summary", ""),
                    comments=data.get("comments", []),
                    security_issues=data.get("security_issues", []),
                    performance_issues=data.get("performance_issues", []),
                    style_issues=data.get("style_issues", []),
                )
        except (json.JSONDecodeError, ValueError):
            pass

        return CodeReview(
            decision=ReviewDecision.CHANGES_REQUESTED,
            summary=f"Review completed but output could not be parsed. Raw output:\n{result_text[:1000]}",
        )

    def _format_review_body(self, review: CodeReview) -> str:
        """Format a CodeReview into a Markdown PR review body."""
        parts = [f"## Code Review\n\n{review.summary}"]

        if review.security_issues:
            parts.append("\n### Security Issues")
            for issue in review.security_issues:
                parts.append(f"- {issue}")

        if review.performance_issues:
            parts.append("\n### Performance Issues")
            for issue in review.performance_issues:
                parts.append(f"- {issue}")

        if review.style_issues:
            parts.append("\n### Style Issues")
            for issue in review.style_issues:
                parts.append(f"- {issue}")

        return "\n".join(parts)
