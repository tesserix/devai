"""Senior Developer Agent — implements a single story on its own feature branch using Claude.

In the collaborative parallel model, the Senior Developer:
1. Receives a single story assignment with its technical plan
2. Creates a feature branch: story/<number>-<slug>
3. Implements only the changes for that story
4. Runs validation checks (compile, lint, tests, format)
5. Creates a PR linked to the story issue
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any

from devai.agents.skills import get_skill_profile
from devai.core.base_agent import BaseAgent
from devai.providers.anthropic_claude import ClaudeProvider
from devai.tools.github_tools import GITHUB_TOOLS, GitHubToolExecutor
from devai.tools.validation_tools import VALIDATION_TOOLS, ValidationToolExecutor

if TYPE_CHECKING:
    from devai.graph.a2a import A2ABus
    from devai.graph.state import ALMState

logger = logging.getLogger(__name__)

# Sr Dev gets full read/write tools + validation tools
SR_DEV_TOOLS = [
    t
    for t in GITHUB_TOOLS
    if t["name"]
    in {
        "github_get_file_content",
        "github_list_files",
        "github_get_repo_tree",
        "github_create_branch",
        "github_commit_file",
        "github_create_pull_request",
        "github_add_comment",
    }
] + VALIDATION_TOOLS

SYSTEM_PROMPT = """You are a Senior Software Engineer implementing a SINGLE user story on its own feature branch.

You are part of a collaborative development team. Multiple stories are being worked on in parallel,
each on its own feature branch. You are assigned ONE story — implement ONLY what that story requires.

Your responsibilities:
1. Read and understand the story's technical plan
2. Explore the existing codebase to understand patterns
3. Create a feature branch named: story/<issue-number>-<short-slug>
4. Implement the code changes file by file
5. Run validation checks
6. Create a pull request linked to the story issue

Implementation guidelines:
- Follow existing code patterns and conventions in the repository
- Write clean, well-structured code
- Include appropriate error handling
- Do NOT add unnecessary comments or documentation
- Do NOT include AI-generated disclaimers or co-authored-by lines
- Keep changes focused — implement ONLY what this story requires
- Do NOT touch files that belong to other stories unless absolutely necessary
- Use consistent naming conventions with the existing codebase

Branch naming: story/<issue-number>-<short-slug>
  Example: story/42-add-user-auth, story/15-payment-integration

Process:
1. Use github_get_repo_tree to understand the full structure
2. Read existing files that you'll modify or that demonstrate patterns
3. Create a feature branch with github_create_branch
4. Implement changes by committing files with github_commit_file
5. Create a pull request with github_create_pull_request

PR title format: "Story #<number>: <title>"
PR body must reference: "Closes #<story-issue-number>"

Important:
- Each commit message should describe what changed and why
- The PR description should reference the story issue
- Never commit secrets, API keys, or credentials
- Ensure all file paths are correct relative to the repo root

## Containerization

If the repo does NOT have a `Dockerfile` at the root, you MUST create one
appropriate for the active skill profile (the profile section below
includes a Dockerfile template — use it as the starting point and adapt
the entrypoint to the actual code you wrote).

- Use a multi-stage build (builder + minimal runtime)
- Pick a slim/distroless runtime image where possible
- Expose the port the application listens on
- Never bake secrets or credentials into the image
- After creating the Dockerfile, validate it with `validate_dockerfile`
  if the tool is available; otherwise just verify the syntax is sane
  and the COPY paths match what you committed

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
    """Implements a single story on its own feature branch using Claude with GitHub tools."""

    name = "senior_developer"
    subscribe_subject = "devai.pipeline.plan_ready"
    publish_subject = "devai.pipeline.code_ready"

    async def _execute_graph(self, state: ALMState, a2a: A2ABus) -> dict[str, Any]:
        """Implement the active story on its own feature branch."""
        claude = ClaudeProvider(self.config)
        github_tools = GitHubToolExecutor(self.github)
        validation_tools = ValidationToolExecutor(self.github)
        observed_pr_number = state.get("pr_number")

        async def tool_executor(tool_name: str, tool_input: dict[str, Any]) -> str:
            if tool_name.startswith("github_"):
                result = await github_tools.execute(tool_name, tool_input)
                nonlocal observed_pr_number
                if tool_name == "github_create_pull_request":
                    observed_pr_number = self._extract_pr_number(result) or observed_pr_number
                return result
            if tool_name.startswith("validate_"):
                return await validation_tools.execute(tool_name, tool_input)
            return f"Unknown tool: {tool_name}"

        repo = state.get("repo_full_name", "")
        plan = state.get("technical_plan", "")
        stories = state.get("stories", [])
        requirements = state.get("requirements", "")
        tech_stack = state.get("detected_tech_stack", "")
        memory_context = state.get("memory_context", "")

        # Get the active story context
        active_idx = state.get("active_story_index", 0)
        active_story = stories[active_idx] if active_idx < len(stories) else {}
        story_number = active_story.get("number", "?")
        story_title = active_story.get("title", "Unknown Story")
        story_desc = active_story.get("description", "")
        acceptance_criteria = active_story.get("acceptance_criteria", [])

        ac_text = "\n".join(f"- {c}" for c in acceptance_criteria) if acceptance_criteria else "(none)"

        # Build the branch name
        slug = self._slugify(story_title)
        branch_name = f"story/{story_number}-{slug}"

        # Check for existing branch (revision iteration)
        existing_branch = state.get("branch_name")
        if existing_branch:
            branch_name = existing_branch

        # Check for A2A messages (escalations from CI, review feedback, etc.)
        inbox_context = a2a.format_inbox_context()

        # Check for escalations from CI Monitor or Security
        escalations = a2a.get_escalations()
        ci_fix_context = ""
        if escalations:
            ci_issues = "\n".join(f"- {e['subject']}: {e['body'][:200]}" for e in escalations)
            ci_fix_context = f"\n\n## Issues to Fix\n{ci_issues}"

        user_message = f"""Repository: {repo}
Run ID: {state.get("run_id", "")}

## YOUR ASSIGNED STORY
Story #{story_number}: {story_title}
Description: {story_desc}

### Acceptance Criteria
{ac_text}

## Technical Plan for This Story
{plan}

## Detected Tech Stack
{tech_stack or "Not detected"}

## Relevant Memory From Past Runs
{memory_context or "(none)"}

## Original Requirements (for context)
{requirements[:1500]}
{ci_fix_context}
{inbox_context}

## Instructions
Create feature branch `{branch_name}`, implement ONLY the changes for Story #{story_number}, and create a PR.
The PR title should be: "Story #{story_number}: {story_title}"
The PR body must include: "Closes #{story_number}"
"""

        # If revision iteration, include review feedback
        review_feedback = state.get("review_feedback", [])
        if review_feedback:
            feedback_text = "\n\n".join(review_feedback)
            user_message += f"""

## Review Feedback (Iteration {state.get("review_iteration", 0)})
The Staff Reviewer requested the following changes. Address ALL of them:
{feedback_text}

Use the existing branch `{branch_name}` — push fixes to it, do NOT create a new branch."""

        system = SYSTEM_PROMPT

        # Inject the skill profile so Claude knows the exact directory
        # layout, test framework, file conventions, and idioms for the
        # detected stack. This is what makes the dev agent behave like a
        # senior engineer in (e.g.) Next.js+React rather than a generic
        # "implement code" assistant. Also inject the infra guidance so
        # the dev knows the right Dockerfile template for the stack and
        # creates one if it's missing.
        profile = get_skill_profile(state.get("skill_profile_name"))
        system += "\n\n" + profile.render_for_developer()
        if profile.infra_guidance:
            system += "\n\n" + profile.render_for_infra()
        logger.info(
            "Senior Developer running with skill profile: %s",
            profile.display_name,
        )

        governance = state.get("governance", "")
        if governance:
            system += f"\n\n## Repository Governance (CLAUDE.md)\nYou MUST follow these rules:\n\n{governance}"

        result_text = await claude.run_agent_loop(
            system_prompt=system,
            user_message=user_message,
            tools=SR_DEV_TOOLS,
            tool_executor=tool_executor,
            max_iterations=self.config.claude_max_iterations_implementation,
        )

        # Notify reviewer that code is ready for this story
        a2a.handoff(
            "staff_reviewer",
            f"Story #{story_number} Ready for Review",
            f"Implementation of Story #{story_number}: {story_title} complete.\n"
            f"Branch: {branch_name}\n\n{result_text[:300]}...",
        )

        # Notify CI Monitor to watch the build
        a2a.notify(
            "ci_monitor",
            f"Story #{story_number} Code Pushed",
            f"New code pushed to branch '{branch_name}' for Story #{story_number}.",
        )

        return {
            "implementation_summary": result_text,
            "branch_name": branch_name,
            "pr_number": observed_pr_number,
        }

    @staticmethod
    def _slugify(title: str) -> str:
        """Convert a story title to a branch-safe slug."""
        slug = title.lower().strip()
        slug = re.sub(r"[^a-z0-9\s-]", "", slug)
        slug = re.sub(r"[\s_]+", "-", slug)
        slug = re.sub(r"-+", "-", slug)
        slug = slug.strip("-")
        return slug[:40]

    @staticmethod
    def _extract_pr_number(tool_output: str) -> int | None:
        try:
            data = json.loads(tool_output)
        except json.JSONDecodeError:
            return None

        for key in ("number", "iid"):
            value = data.get(key)
            if isinstance(value, int):
                return value
        return None
