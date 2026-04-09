"""Staff Developer/Reviewer Agent — reviews code using Claude tool-use loop."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from devai.agents.skills import get_skill_profile
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
        """Review the PR code for the active story using Claude tool-use loop."""
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

        # Get active story context
        active_idx = state.get("active_story_index", 0)
        stories = state.get("stories", [])
        active_story = stories[active_idx] if active_idx < len(stories) else {}
        story_number = active_story.get("number", "?")
        story_title = active_story.get("title", "")
        acceptance_criteria = active_story.get("acceptance_criteria", [])

        ac_text = "\n".join(f"- {c}" for c in acceptance_criteria) if acceptance_criteria else "(none)"

        # Check for messages from other agents
        inbox_context = a2a.format_inbox_context()

        review_system = """You are a Staff Software Engineer performing a thorough code review.

You are reviewing a PR for a SINGLE user story. The story was implemented on its own feature branch.
Ensure the implementation matches the story's acceptance criteria and doesn't include unrelated changes.

Focus on:
1. Code quality and adherence to project conventions
2. Story completeness: does the PR satisfy ALL acceptance criteria?
3. Scope: does the PR contain ONLY changes for this story (no scope creep)?
4. Performance: unnecessary allocations, N+1 queries, blocking calls
5. Security: injection, auth bypass, secrets in code, OWASP Top 10
6. Error handling: proper error propagation, no swallowed errors
7. Testing: are the changes testable? Any obvious missing test cases?

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

        review_prompt = f"""Review this pull request for Story #{story_number}: {story_title}

PR #{pr_number} on {repo}
Branch: {branch}

## Story Details
Title: {story_title}
Description: {active_story.get("description", "")[:500]}

### Acceptance Criteria
{ac_text}

## Requirements
{state.get("requirements", "")[:1500]}

{inbox_context}

## Relevant Memory From Past Runs
{state.get("memory_context", "") or "(none)"}

Start by getting the PR diff with github_get_pr_diff, then explore the repo structure and read relevant files to understand the codebase context. Verify the implementation satisfies the acceptance criteria."""

        # Inject the skill-specific review checklist so the reviewer
        # checks the things that actually matter for the detected stack
        # (e.g. "no useEffect for fetching" for React, "context.Context
        # first arg" for Go).
        profile = get_skill_profile(state.get("skill_profile_name"))
        review_system += "\n\n" + profile.render_for_reviewer()
        logger.info("Staff Reviewer running with skill profile: %s", profile.display_name)

        governance = state.get("governance", "")
        if governance:
            review_system += f"\n\n## Repository Governance (CLAUDE.md)\nYou MUST follow these rules:\n\n{governance}"

        result_text = await claude.run_agent_loop(
            system_prompt=review_system,
            user_message=review_prompt,
            tools=self.REVIEWER_TOOLS,
            tool_executor=tool_executor.execute,
            max_iterations=self.config.claude_max_iterations_review,
        )

        review = self._parse_review_text(result_text)

        # Post the review on the PR — degrade gracefully if the PR has
        # already been merged/closed (otherwise GitHub returns 422 and we
        # would fail the entire pipeline run for an issue that's already
        # resolved). Also catch any other 422 from the reviews endpoint
        # (e.g. "can not approve your own pull request") and treat it as
        # a soft skip with a warning rather than a hard failure.
        event = "APPROVE" if review.decision == ReviewDecision.APPROVED else "REQUEST_CHANGES"
        review_body = self._format_review_body(review)

        pr_already_resolved = False
        try:
            pr_info = await self.github.get_pull_request(repo, pr_number)
            pr_state = (pr_info.get("state") or "").lower()
            pr_merged = bool(pr_info.get("merged"))
            if pr_state == "closed" or pr_merged:
                pr_already_resolved = True
                logger.info(
                    "Skipping review on PR #%s — already %s",
                    pr_number,
                    "merged" if pr_merged else pr_state,
                )
        except Exception as e:
            logger.warning("Could not fetch PR #%s state before review: %s", pr_number, e)

        if not pr_already_resolved:
            try:
                await self.github.create_pr_review(
                    repo,
                    pr_number,
                    review_body,
                    event,
                )
            except Exception as e:
                msg = str(e)
                if "422" in msg or "Unprocessable" in msg:
                    logger.warning(
                        "PR review on #%s rejected by GitHub (%s) — treating as already resolved",
                        pr_number,
                        msg.splitlines()[0][:200],
                    )
                    pr_already_resolved = True
                else:
                    raise

        # If the PR was already resolved, force the decision to APPROVED so
        # the pipeline can hand off to the next agent instead of looping.
        if pr_already_resolved:
            review.decision = ReviewDecision.APPROVED
            review.summary = (
                review.summary
                + "\n\n_Note: PR was already merged/closed when the reviewer ran. "
                "Treating as approved._"
            )

        # A2A communication based on review decision — include story context
        if review.decision == ReviewDecision.APPROVED:
            a2a.handoff(
                "security_expert",
                f"Story #{story_number} Approved — Security Scan Next",
                f"PR #{pr_number} for Story #{story_number} approved. Proceed with security scan.",
            )
            a2a.notify(
                "qa_tester",
                f"Story #{story_number} Approved",
                f"PR #{pr_number} for Story #{story_number} approved by review. Prepare for testing.",
            )
        else:
            a2a.escalate(
                "senior_developer",
                f"Story #{story_number} — Changes Requested",
                f"Review of PR #{pr_number} (Story #{story_number}) requested changes:\n\n{review.summary}",
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
