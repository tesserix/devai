"""Engineering Manager Agent — analyzes user stories and creates technical plans using Claude."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from devai.core.base_agent import BaseAgent
from devai.providers.anthropic_claude import ClaudeProvider
from devai.tools.github_tools import GITHUB_TOOLS, GitHubToolExecutor

if TYPE_CHECKING:
    from devai.graph.a2a import A2ABus
    from devai.graph.state import ALMState

logger = logging.getLogger(__name__)

# EM gets read-only + comment tools
EM_TOOLS = [
    t
    for t in GITHUB_TOOLS
    if t["name"]
    in {
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

    async def _execute_graph(self, state: ALMState, a2a: A2ABus) -> dict[str, Any]:
        """Analyze stories and create a technical plan."""
        claude = ClaudeProvider(self.config)
        tool_executor = GitHubToolExecutor(self.github)

        repo = state.get("repo_full_name", "")
        stories = state.get("stories", [])
        requirements = state.get("requirements", "")
        analyzed_reqs = state.get("analyzed_requirements", [])

        # Build story references
        issue_refs = "\n".join(
            f"- #{s.get('number', '?')}: {s.get('title', '')} (priority: {s.get('priority', 'medium')})"
            for s in stories
        )

        # Build requirement context
        req_context = ""
        if analyzed_reqs:
            req_context = "\n## Analyzed Requirements\n" + "\n".join(
                f"- [{r.get('category', 'functional')}] {r.get('title', '')}" for r in analyzed_reqs[:10]
            )

        # Check for A2A messages (handoffs, gaps, etc.)
        inbox_context = a2a.format_inbox_context()

        user_message = f"""Repository: {repo}

## User Stories to Implement
{issue_refs}

## Original Requirements
{requirements[:2000]}
{req_context}

{inbox_context}

Please analyze the repository structure and these user stories, then create a detailed technical implementation plan.

Start by exploring the repo structure, then read relevant files to understand the codebase patterns."""

        # If this is a revision after review feedback
        review_feedback = state.get("review_feedback", [])
        if review_feedback:
            feedback_text = "\n\n".join(review_feedback)
            user_message += f"""

## Review Feedback from Previous Iteration
The Staff Reviewer requested changes. Please incorporate this feedback into your revised plan:
{feedback_text}"""

        # Inject governance
        system = SYSTEM_PROMPT
        governance = state.get("governance", "")
        if governance:
            system += f"\n\n## Repository Governance (CLAUDE.md)\nYou MUST follow these rules:\n\n{governance}"

        plan_text = await claude.run_agent_loop(
            system_prompt=system,
            user_message=user_message,
            tools=EM_TOOLS,
            tool_executor=tool_executor.execute,
        )

        # Post plan as a comment on the first story
        if stories:
            first_issue = stories[0].get("number")
            if first_issue:
                await self.github.add_comment(
                    repo,
                    first_issue,
                    f"## Technical Implementation Plan\n\n{plan_text}",
                )

        # Handoff to Senior Developer via A2A
        a2a.handoff(
            "senior_developer",
            "Technical Plan Ready",
            f"Technical plan created for {len(stories)} stories.\n\n{plan_text[:500]}...",
            payload={"stories_count": len(stories)},
        )

        # Notify QA about what to expect
        a2a.notify(
            "qa_tester",
            "Implementation Starting",
            f"Technical plan approved for {len(stories)} stories. Prepare test strategy based on the requirements.",
        )

        return {
            "technical_plan": plan_text,
        }
