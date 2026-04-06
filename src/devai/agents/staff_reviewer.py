"""Staff Developer/Reviewer Agent — reviews code using OpenAI Codex sandbox."""

from __future__ import annotations

import json
import logging
from typing import Any

from devai.core.base_agent import BaseAgent
from devai.graph.a2a import A2ABus
from devai.graph.state import ALMState
from devai.models import CodeReview, ReviewDecision
from devai.providers.openai_codex import CodexSandboxProvider

logger = logging.getLogger(__name__)


class StaffReviewerAgent(BaseAgent):
    """Reviews code for standards, optimization, and security using Codex sandbox."""

    name = "staff_reviewer"
    subscribe_subject = "devai.pipeline.code_ready"
    publish_subject = ""

    async def _execute_graph(self, state: ALMState, a2a: A2ABus) -> dict[str, Any]:
        """Review the PR code using Codex sandbox."""
        codex = CodexSandboxProvider(self.config)

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

        # Get the PR diff
        diff = await self.github.get_pr_diff(repo, pr_number)

        # Check for messages from other agents
        inbox_context = a2a.format_inbox_context()

        review_prompt = f"""Review this pull request:

PR #{pr_number} on {repo}
Branch: {branch}

## PR Diff
```
{diff[:10000]}
```

## Requirements
{state.get('requirements', '')[:2000]}

{inbox_context}

Focus on:
1. Code quality and adherence to project conventions
2. Performance: unnecessary allocations, N+1 queries, blocking calls
3. Security: injection, auth bypass, secrets in code, OWASP Top 10
4. Error handling: proper error propagation, no swallowed errors
5. Testing: are the changes testable? Any obvious missing test cases?

Be thorough but fair. Only request changes for genuine issues, not style preferences."""

        result = await codex.run_review(
            repo_url=f"https://github.com/{repo}.git",
            branch=branch,
            review_prompt=review_prompt,
        )

        review = self._parse_review(result)

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

    def _parse_review(self, codex_result: dict[str, Any]) -> CodeReview:
        """Parse Codex sandbox output into a structured CodeReview."""
        output = codex_result.get("output", "")

        try:
            start = output.find("{")
            end = output.rfind("}") + 1
            if start >= 0 and end > start:
                data = json.loads(output[start:end])
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

        if not codex_result.get("success", False):
            return CodeReview(
                decision=ReviewDecision.CHANGES_REQUESTED,
                summary=f"Review failed: {codex_result.get('error', 'Unknown error')}",
            )

        return CodeReview(
            decision=ReviewDecision.CHANGES_REQUESTED,
            summary=f"Review completed but output could not be parsed. Raw output:\n{output[:1000]}",
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
