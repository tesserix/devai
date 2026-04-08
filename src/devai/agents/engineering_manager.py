"""Engineering Manager Agent — creates per-story technical plans using Claude.

In the collaborative parallel model, the EM acts as a senior architect who:
1. Analyzes the full repository structure and codebase patterns
2. Creates individual technical plans for each user story
3. Assigns file ownership per story to avoid merge conflicts
4. Ensures stories can be implemented independently on separate branches
"""

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

You work in a **collaborative parallel model** — each user story will be implemented on its own feature branch by a developer, reviewed independently, and merged separately. Your job is to create a plan that enables this parallelism.

Your responsibilities:
1. Analyze the repository structure and existing codebase patterns
2. Create an INDIVIDUAL technical plan for EACH user story
3. Assign file ownership — which files each story should create or modify
4. Ensure stories can be implemented independently without merge conflicts

Process:
1. Use github_get_repo_tree to understand the repo structure
2. Read key files to understand patterns, frameworks, and conventions
3. Read the user story issues for full context
4. Create per-story technical plans

For EACH story, your plan must include:
- Story number and title reference
- Summary of what needs to be built for this story
- List of files to create or modify (with full paths)
- Detailed implementation approach
- Dependencies on other stories (if any — minimize these)
- Estimated complexity (low/medium/high)

CRITICAL RULES:
- Minimize file overlap between stories. If two stories need to modify the same file,
  note it explicitly and describe how to handle the merge.
- Each story plan must be self-contained — a developer should be able to implement it
  without seeing the other story plans.
- Consider the existing code patterns and conventions in the repo.
- Think about test requirements for each story.

Output format — separate each story plan with a clear delimiter:

=== STORY PLAN: #<number> — <title> ===
[plan content]
=== END STORY PLAN ===

After all story plans, include a brief "Integration Notes" section covering:
- File ownership conflicts (which files are touched by multiple stories)
- Suggested merge order (which PRs should merge first)
- Any shared infrastructure changes needed"""


class EngineeringManagerAgent(BaseAgent):
    """Creates per-story technical implementation plans using Claude."""

    name = "engineering_manager"
    subscribe_subject = "devai.pipeline.stories_ready"
    publish_subject = "devai.pipeline.plan_ready"

    async def _execute_graph(self, state: ALMState, a2a: A2ABus) -> dict[str, Any]:
        """Analyze stories and create per-story technical plans."""
        claude = ClaudeProvider(self.config)
        tool_executor = GitHubToolExecutor(self.github)

        repo = state.get("repo_full_name", "")
        stories = state.get("stories", [])
        requirements = state.get("requirements", "")
        analyzed_reqs = state.get("analyzed_requirements", [])

        # Build story references
        story_details = []
        for s in stories:
            ac = s.get("acceptance_criteria", [])
            ac_text = "\n".join(f"  - {c}" for c in ac) if ac else "  (none specified)"
            story_details.append(
                f"### Story #{s.get('number', '?')}: {s.get('title', '')}\n"
                f"Priority: {s.get('priority', 'medium')}\n"
                f"Description: {s.get('description', '')[:300]}\n"
                f"Acceptance Criteria:\n{ac_text}"
            )
        stories_text = "\n\n".join(story_details)

        # Build requirement context
        req_context = ""
        if analyzed_reqs:
            req_context = "\n## Analyzed Requirements\n" + "\n".join(
                f"- [{r.get('category', 'functional')}] {r.get('title', '')}" for r in analyzed_reqs[:10]
            )

        # Check for A2A messages
        inbox_context = a2a.format_inbox_context()

        user_message = f"""Repository: {repo}

## User Stories to Plan (each will get its own feature branch)
{stories_text}

## Original Requirements
{requirements[:2000]}
{req_context}

{inbox_context}

Create an individual technical plan for EACH story above. Each story will be implemented on a separate feature branch (story/<number>-<slug>) by a developer working independently.

Start by exploring the repo structure, then read relevant files to understand the codebase patterns. Then create per-story plans that minimize file conflicts."""

        # If this is a revision after review feedback
        review_feedback = state.get("review_feedback", [])
        if review_feedback:
            feedback_text = "\n\n".join(review_feedback)
            user_message += f"""

## Review Feedback from Previous Iteration
The Staff Reviewer requested changes. Incorporate this feedback into revised plans:
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

        # Parse per-story plans from the output
        story_plans = self._parse_story_plans(plan_text, len(stories))

        # Post plans as comments on each story issue
        for i, story in enumerate(stories):
            issue_num = story.get("number")
            if issue_num and i < len(story_plans):
                await self.github.add_comment(
                    repo,
                    issue_num,
                    f"## Technical Implementation Plan\n\n{story_plans[i]}",
                )

        # Handoff to developers via A2A — each story will be picked up independently
        a2a.broadcast(
            "Per-Story Technical Plans Ready",
            f"Created {len(story_plans)} individual technical plans for {len(stories)} stories.\n"
            f"Each story will be implemented on its own feature branch.\n\n"
            + "\n".join(
                f"- Story #{s.get('number', '?')}: {s.get('title', '')}" for s in stories[:10]
            ),
        )

        # Notify QA about what to expect
        a2a.notify(
            "qa_tester",
            "Story Implementation Starting",
            f"Technical plans approved for {len(stories)} stories. "
            "Each story will have its own PR — prepare per-story test strategies.",
        )

        return {
            "technical_plan": plan_text,
            "story_plans": story_plans,
        }

    def _parse_story_plans(self, plan_text: str, num_stories: int) -> list[str]:
        """Parse individual story plans from the EM output.

        Looks for delimiters like:
            === STORY PLAN: #<number> — <title> ===
            [content]
            === END STORY PLAN ===
        """
        import re

        # Try structured delimiter parsing
        pattern = r"===\s*STORY PLAN.*?===\s*\n(.*?)===\s*END STORY PLAN\s*==="
        matches = re.findall(pattern, plan_text, re.DOTALL)

        if matches and len(matches) >= num_stories:
            return [m.strip() for m in matches[:num_stories]]

        # Fallback: try splitting on story number headers
        # Look for patterns like "### Story #1" or "## Story #42" or "# Story Plan: #1"
        parts = re.split(r"(?=#{1,3}\s*Story\s+(?:Plan[:\s]*)?#\d+)", plan_text)
        parts = [p.strip() for p in parts if p.strip()]

        if len(parts) >= num_stories:
            return parts[:num_stories]

        # Last resort: give the full plan to every story
        if num_stories > 0:
            return [plan_text] * num_stories

        return []
