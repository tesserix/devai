"""Staff Developer/Reviewer Agent — reviews code using OpenAI Codex sandbox."""

from __future__ import annotations

import json
import logging
from typing import Any

from devai.core.base_agent import BaseAgent
from devai.models import (
    AgentResult,
    CodeReview,
    PipelineContext,
    PipelineStage,
    ReviewDecision,
)
from devai.core.pipeline import PipelineOrchestrator
from devai.providers.openai_codex import CodexSandboxProvider

logger = logging.getLogger(__name__)


class StaffReviewerAgent(BaseAgent):
    """Reviews code for standards, optimization, and security using Codex sandbox."""

    name = "staff_reviewer"
    subscribe_subject = "devai.pipeline.code_ready"
    publish_subject = ""  # Routing handled manually based on review decision

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.codex = CodexSandboxProvider(self.config)

    async def execute(self, ctx: PipelineContext) -> AgentResult:
        """Review the PR code using Codex sandbox."""
        pr_number = ctx.pr_number
        branch = ctx.branch_name
        repo = ctx.repo_full_name

        if not pr_number or not branch:
            return AgentResult(
                agent_name=self.name,
                status="failed",
                summary="No PR or branch found in pipeline context",
            )

        # Get the PR diff for context
        diff = await self.github.get_pr_diff(repo, pr_number)

        review_prompt = f"""Review this pull request:

PR #{pr_number} on {repo}
Branch: {branch}

## PR Diff
```
{diff[:10000]}
```

## Requirements
{ctx.requirements}

Focus on:
1. Code quality and adherence to project conventions
2. Performance: unnecessary allocations, N+1 queries, blocking calls
3. Security: injection, auth bypass, secrets in code, OWASP Top 10
4. Error handling: proper error propagation, no swallowed errors
5. Testing: are the changes testable? Any obvious missing test cases?

Be thorough but fair. Only request changes for genuine issues, not style preferences."""

        # Run review in Codex sandbox
        result = await self.codex.run_review(
            repo_url=f"https://github.com/{repo}.git",
            branch=branch,
            review_prompt=review_prompt,
        )

        # Parse the review output
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

        # Route based on decision using the pipeline orchestrator
        orchestrator = PipelineOrchestrator(self.event_bus, self.state, self.config)
        await orchestrator.handle_review_decision(
            ctx=ctx,
            decision=review.decision,
            feedback=review.summary,
        )

        return AgentResult(
            agent_name=self.name,
            status="success",
            output={
                "decision": review.decision.value,
                "summary": review.summary,
                "security_issues": review.security_issues,
                "performance_issues": review.performance_issues,
                "comments_count": len(review.comments),
            },
            summary=f"Review: {review.decision.value} — {review.summary}",
        )

    def _parse_review(self, codex_result: dict[str, Any]) -> CodeReview:
        """Parse Codex sandbox output into a structured CodeReview."""
        output = codex_result.get("output", "")

        # Try to extract JSON from the output
        try:
            # Find JSON in the output
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

        # Fallback: if Codex failed or output isn't parseable
        if not codex_result.get("success", False):
            return CodeReview(
                decision=ReviewDecision.CHANGES_REQUESTED,
                summary=f"Review failed: {codex_result.get('error', 'Unknown error')}",
            )

        # Default: request changes if we can't parse
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
