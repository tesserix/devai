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

## Lane Boundaries — STRICT (failure to comply means the run is rejected)

You ONLY edit application source code. You NEVER touch any of the following:

- `.github/` and ANY file under it (workflows, issue templates, dependabot config, etc.)
  → That is the CI Engineer agent's lane. If CI is missing or broken, ESCALATE via the
  A2A bus to the ci_monitor agent. Do NOT create or edit `.github/workflows/*.yml` yourself.
- `Dockerfile`, `docker-compose.yml`, `docker-compose.yaml`
  → Infra Provisioner's lane. If you need a container image, escalate to infra_provisioner.
- `helm/`, `chart/`, `k8s/`, `manifests/`, any `Chart.yaml` or kustomization
  → Infra Provisioner's lane.
- Database migration files (`migrations/`, `alembic/`, `prisma/migrations/`)
  → DB Engineer's lane. You can define ORM models in the source tree, but NEVER write
  raw migration SQL or auto-generate migration scripts.
- Test infrastructure config that already exists (vitest.config, jest.config, pytest.ini)
  → Edit only if your story explicitly says so. Otherwise leave it alone.
- The repository's `CLAUDE.md`, `CONTRIBUTING.md`, or top-level governance docs
  → Read-only. These are inputs to your work, not outputs.

What you DO own:
- All application source files under `src/`, `app/`, `lib/`, `components/`, `pages/`,
  `internal/`, `cmd/`, `apps/`, etc. (whatever the SkillProfile prescribes for the stack)
- Co-located unit tests for the code you write (e.g. `*.test.tsx`, `*_test.go`,
  `tests/test_*.py`)
- Package manifests (`package.json`, `pyproject.toml`, `go.mod`, `Cargo.toml`) — but ONLY
  to add dependencies your story actually needs. Never bump unrelated versions.
- A `Dockerfile` IF the repo doesn't have one yet (the SkillProfile section below tells
  you the right template). If a Dockerfile already exists, leave it alone — that's the
  Infra Provisioner's territory.

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


    _UI_HINTS = (
        "ui", "ux", "frontend", "component", "page", "layout", "design", "css",
        "tailwind", "responsive", "storefront", "navigation", "form", "modal",
    )

    def _model_for_story(self, state: ALMState) -> str | None:
        """Pick the implementation model from the active story's nature.

        UI work → config.llm_model_dev_ui (claude-fable-5: strongest design
        intuition); everything else → config.llm_model_dev_api
        (claude-opus-4-8: deepest coding). Empty config → provider default.
        """
        stories = state.get("stories") or []
        idx = state.get("active_story_index", 0)
        story = stories[idx] if isinstance(stories, list) and idx < len(stories) else {}
        haystack = " ".join(
            str(x).lower()
            for x in (
                story.get("title", ""),
                story.get("description", "")[:300] if isinstance(story.get("description"), str) else "",
                " ".join(story.get("skills") or []) if isinstance(story.get("skills"), list) else "",
                " ".join(story.get("labels") or []) if isinstance(story.get("labels"), list) else "",
            )
        )
        is_ui = any(h in haystack for h in self._UI_HINTS)
        field = "llm_model_dev_ui" if is_ui else "llm_model_dev_api"
        return getattr(self.config, field, None) or None

    async def _execute_graph(self, state: ALMState, a2a: A2ABus) -> dict[str, Any]:
        """Implement the active story on its own feature branch."""
        # Model routing: UI/frontend stories get the design-strongest model
        # (claude-fable-5); API/backend/data stories get the deep-coding one
        # (claude-opus-4-8). Routed per STORY, not per run — a full-stack
        # epic uses the right specialist model for each piece.
        claude = ClaudeProvider(self.config, model=self._model_for_story(state))
        # Pre-wire the executor with run_id + redis + identity so every
        # scm_commit_file call shows up in the dashboard's REPO tab in
        # real time, attributed to this agent + the originating user.
        github_tools = GitHubToolExecutor(
            self.github,
            agent_name=self.name,
            run_id=state.get("run_id", ""),
            redis=getattr(self.state, "redis", None),
            triggered_by=state.get("trigger_actor", "") or "",
            trace_id=state.get("trace_id", "") or "",
        )
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
        story_number = active_story.get("number")
        story_title = active_story.get("title", "")
        story_desc = active_story.get("description", "")
        acceptance_criteria = active_story.get("acceptance_criteria", [])

        ac_text = "\n".join(f"- {c}" for c in acceptance_criteria) if acceptance_criteria else "(none)"

        # Build the branch name. When this agent runs inside an ALM run it has a
        # numbered story → story/<n>-<slug>. But the app-scaffold blueprint
        # reuses this agent with NO story, so story_number was "?" and the title
        # defaulted to "Unknown Story", yielding the branch "story/?-unknown-story".
        # The "?" is an INVALID git ref and later made spin_preview_pod raise
        # ValueError → the whole run FAILED even though scaffolding succeeded.
        # Fall back to a ref-safe, story-less branch in that case.
        slug = self._slugify(story_title)
        if story_number:
            branch_name = f"story/{story_number}-{slug}"
        else:
            run_suffix = str(state.get("run_id", ""))[-8:].strip("-")
            branch_name = f"devai/{slug}" if slug else f"devai/scaffold-{run_suffix or 'app'}"

        # Check for existing branch (revision iteration)
        existing_branch = state.get("branch_name")
        if existing_branch:
            branch_name = existing_branch

        # Resume-awareness: a previous ATTEMPT of this stage (timeout, pod
        # restart, retry, heal) may already have created a branch and
        # committed files. Without this the agent restarts FROM SCRATCH
        # every attempt — a live run re-committed the same configs three
        # times across resumes and could never beat the stage timeout.
        run_id_for_branch = str(state.get("run_id") or "")
        redis = getattr(self.state, "redis", None)
        prior_workbranch = ""
        committed_paths: list[str] = []
        if run_id_for_branch and redis is not None:
            try:
                prior_workbranch = str(
                    await redis.get(f"devai:run:{run_id_for_branch}:workbranch") or ""
                )
                raw_events = await redis.lrange(
                    f"devai:run:{run_id_for_branch}:repo_events", -300, -1
                )
                seen: set[str] = set()
                for entry in raw_events:
                    try:
                        p = json.loads(entry).get("path") or ""
                    except Exception:  # noqa: BLE001
                        continue
                    if p and p not in seen:
                        seen.add(p)
                        committed_paths.append(p)
            except Exception:  # noqa: BLE001 — resume context is best-effort
                logger.debug("resume context read failed", exc_info=True)
        # Ground truth wins: continue on the branch real commits landed on,
        # even when the previous attempt's LLM picked its own branch name.
        if prior_workbranch:
            branch_name = prior_workbranch

        # Record the working branch durably the MOMENT it's known — the
        # run's branch_name only lands when this stage completes, so
        # without this the dashboard's REPO tab keeps showing the old
        # branch for the whole (long) implement stage while commits pile
        # up somewhere the user can't see. (The tool layer re-stamps this
        # with the actual branch on every real commit.)
        if run_id_for_branch and redis is not None:
            try:
                await redis.set(
                    f"devai:run:{run_id_for_branch}:workbranch", branch_name, ex=86400 * 7
                )
            except Exception:  # noqa: BLE001 — visibility aid, never fail the stage
                logger.debug("workbranch record failed", exc_info=True)

        # Check for A2A messages (escalations from CI, review feedback, etc.)
        inbox_context = a2a.format_inbox_context()

        # Check for escalations from CI Monitor or Security
        escalations = a2a.get_escalations()
        ci_fix_context = ""
        if escalations:
            ci_issues = "\n".join(f"- {e['subject']}: {e['body'][:200]}" for e in escalations)
            ci_fix_context = f"\n\n## Issues to Fix\n{ci_issues}"

        # Brief the assignment + PR conventions. With NO resolved story
        # (scaffold runs, or older runs where story content didn't survive
        # the handover) the templates previously interpolated None — PRs
        # landed titled "Story #None:" with "Closes #None".
        if story_number:
            assignment = f"""## YOUR ASSIGNED STORY
Story #{story_number}: {story_title}
Description: {story_desc}

### Acceptance Criteria
{ac_text}"""
            pr_instructions = f"""Create feature branch `{branch_name}`, implement ONLY the changes for Story #{story_number}, and create a PR.
The PR title should be: "Story #{story_number}: {story_title}"
The PR body must include: "Closes #{story_number}\""""
        else:
            epic_number = state.get("epic_issue_number")
            assignment = f"""## YOUR ASSIGNMENT
{story_title or "Implement the requirements below end to end."}"""
            pr_instructions = (
                f"Create feature branch `{branch_name}`, implement the changes, and create a PR "
                "with a concise, descriptive title summarizing the change (NEVER reference a "
                "story number you don't have)."
                + (f'\nThe PR body must include: "Part of #{epic_number}"' if epic_number else "")
            )

        user_message = f"""Repository: {repo}
Run ID: {state.get("run_id", "")}

{assignment}

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
{pr_instructions}
"""

        # Continuation brief: never redo finished work. This is what makes
        # retries/resumes INCREMENTAL — each attempt finishes the remaining
        # files instead of burning the whole time budget re-creating what
        # the previous attempt already committed.
        if committed_paths:
            done_list = "\n".join(f"- {p}" for p in committed_paths[:80])
            user_message += f"""

## ALREADY DONE — CONTINUE, DO NOT START OVER
A previous attempt of this stage already committed these files to `{branch_name}`:
{done_list}

The branch EXISTS. Do NOT recreate the branch or re-commit these files unless
one is actually broken. Start by listing the branch to confirm its state, then
implement ONLY the remaining work and finish with the PR."""

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
