"""Product Director Agent — creates Epics and User Stories from analyzed requirements.

Operates in two phases within the LangGraph:
1. run_epic(): Creates a GitHub Epic (tracking issue) from requirements
2. run_stories(): Breaks the epic into individual user stories as GitHub issues
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from devai.core.base_agent import BaseAgent
from devai.graph.a2a import A2ABus

# Primary: OpenAI | Fallback: Claude
from devai.providers.openai_provider import OpenAIProvider

# Groq available as fallback: from devai.providers.groq_provider import GroqProvider

if TYPE_CHECKING:
    from devai.graph.state import ALMState

logger = logging.getLogger(__name__)

EPIC_SYSTEM_PROMPT = """You are a Senior Product Director. Create a GitHub Epic from analyzed requirements.

An Epic is a large body of work that groups multiple user stories.

Output ONLY valid JSON:
{
    "title": "Epic title — concise and descriptive",
    "description": "Detailed epic description with context, goals, and scope",
    "labels": ["epic", "feature-area"],
    "milestones": ["key milestone 1", "key milestone 2"]
}"""

STORIES_SYSTEM_PROMPT = """You are a Senior Product Director at a world-class software company.

Break down the epic into well-structured user stories.

Output ONLY valid JSON — an array of user story objects:
[
  {
    "title": "User story title",
    "description": "As a [user type], I want [goal] so that [benefit].\\n\\nContext: ...",
    "acceptance_criteria": ["Given X, when Y, then Z", ...],
    "priority": "high",
    "labels": ["feature", "frontend"]
  }
]

Guidelines:
- Each story should be independently deliverable
- Stories should be small enough for 1-3 days of work
- Acceptance criteria must be specific and testable
- Include edge cases and error scenarios
- Use "As a [user], I want [goal], so that [benefit]" format"""


class ProductDirectorAgent(BaseAgent):
    """Creates Epics and User Stories using OpenAI Chat Completions API."""

    name = "product_director"
    subscribe_subject = "devai.pipeline.trigger"
    publish_subject = "devai.pipeline.stories_ready"

    async def _execute_graph(self, state: ALMState, a2a: A2ABus) -> dict[str, Any]:
        """Stage-aware dispatch. The blueprint adapter calls the generic
        run() for BOTH the create-epic and create-stories stages — the old
        default always ran run_stories, so blueprint runs never created an
        epic at all (no epic issue, no story linking, nothing to supervise).
        Route on the stage name; default to stories for legacy callers."""
        stage = str(state.get("stage") or "").replace("-", "_").lower()
        if "epic" in stage:
            return await self.run_epic(state, a2a)
        return await self.run_stories(state, a2a)

    async def run_epic(self, state: ALMState, a2a: A2ABus | None = None) -> dict[str, Any]:
        """Create a GitHub Epic from analyzed requirements."""
        openai = OpenAIProvider(self.config)
        if a2a is None:
            a2a = A2ABus(self.name, state.get("a2a_messages", []))

        repo = state.get("repo_full_name", "")
        requirements = state.get("requirements", "")
        analyzed = state.get("analyzed_requirements", [])

        # Check for handoffs from Requirements Analyst
        inbox_context = a2a.format_inbox_context()

        req_summary = ""
        if analyzed:
            req_summary = "\n".join(
                f"- [{r.get('priority', 'medium')}] {r.get('title', '')}: {r.get('description', '')[:100]}"
                for r in analyzed[:15]
            )
        else:
            req_summary = requirements

        # Pull the active skill profile so the epic reflects how this
        # stack actually breaks down work.
        from devai.agents.skills import get_skill_profile

        profile = get_skill_profile(state.get("skill_profile_name"))

        prompt = f"""Repository: {repo}

{profile.render_for_planner()}

## Analyzed Requirements
{req_summary}

## Raw Requirements
{requirements[:2000]}

{inbox_context}

Create a GitHub Epic that encompasses all these requirements. The epic
should reflect the planning guidance above for this stack."""

        response = await openai.generate(
            prompt=prompt,
            system=EPIC_SYSTEM_PROMPT,
            response_format={"type": "json_object"},
        )

        try:
            epic_data = json.loads(response)
        except json.JSONDecodeError:
            epic_data = {"title": "Feature Epic", "description": requirements[:500], "labels": ["epic"]}

        # Ensure title is valid (AI sometimes returns empty/None)
        if not epic_data.get("title", "").strip():
            epic_data["title"] = f"Epic: {requirements[:80]}" if requirements else "Feature Epic"

        # Create the Epic as a GitHub issue
        labels = [lbl for lbl in epic_data.get("labels", []) if isinstance(lbl, str)] + ["epic", "devai:epic"]
        run_id = str(state.get("run_id") or "")
        if run_id:
            labels.append(f"devai:run:{run_id.removeprefix('devai-')[:10]}")
        body = f"{epic_data.get('description', '')}\n\n## Milestones\n"
        for m in epic_data.get("milestones", []):
            body += f"\n- [ ] {m}"

        # Idempotent: scope dedup search to "devai:epic" so we re-use any
        # existing epic for the same intent instead of spamming new ones.
        # Falls back to create_issue if the SCM client doesn't expose
        # the idempotent helper (defensive — we shouldn't ship a client
        # without it but the agent shouldn't fail the run either way).
        issue = await self._create_issue_safe(
            repo=repo,
            title=epic_data.get("title", "Feature Epic"),
            body=body,
            labels=labels,
            dedupe_labels=["devai:epic"],
        )

        epic_result = {
            "title": epic_data.get("title", ""),
            "description": epic_data.get("description", ""),
            "labels": labels,
            "issue_number": issue["number"],
            "url": issue["html_url"],
        }

        # Add epic to the Supervisor's project board
        project_id = state.get("supervisor_project_id")
        if project_id:
            await self._add_to_project(repo, project_id, issue["number"])

        # Notify Engineering Manager
        a2a.notify(
            "engineering_manager",
            "Epic Created",
            f"Epic #{issue['number']}: {epic_data.get('title', '')}\n{issue['html_url']}",
        )

        result: dict[str, Any] = {
            "epics": [epic_result],
            "epic_issue_number": issue["number"],
            # a2a messages are merged by BaseAgent.run() automatically
        }
        return result

    async def run_stories(self, state: ALMState, a2a: A2ABus | None = None) -> dict[str, Any]:
        """Create user stories from the epic and requirements."""
        openai = OpenAIProvider(self.config)
        if a2a is None:
            a2a = A2ABus(self.name, state.get("a2a_messages", []))

        repo = state.get("repo_full_name", "")
        requirements = state.get("requirements", "")
        analyzed = state.get("analyzed_requirements", [])
        epics = state.get("epics", [])
        epic_number = state.get("epic_issue_number")

        req_summary = ""
        if analyzed:
            parts: list[str] = []
            for r in analyzed[:15]:
                if isinstance(r, dict):
                    parts.append(
                        f"- [{r.get('priority', 'medium')}] {r.get('title', '')}: {r.get('description', '')[:150]}"
                    )
                else:
                    parts.append(f"- {str(r)[:200]}")
            req_summary = "\n".join(parts)
        else:
            req_summary = requirements

        epic_context = ""
        if epics:
            ep = epics[0]
            if isinstance(ep, dict):
                epic_context = f"\n## Parent Epic\n{ep.get('title', '')}\n{ep.get('description', '')[:300]}"
            else:
                epic_context = f"\n## Parent Epic\n{str(ep)[:300]}"

        # Pull the active skill profile so stories follow the stack's
        # natural breakdown (component-per-story for React, resource-per-
        # story for Go/Python services).
        from devai.agents.skills import get_skill_profile

        profile = get_skill_profile(state.get("skill_profile_name"))

        prompt = f"""Repository: {repo}

{profile.render_for_planner()}

## Requirements
{req_summary}
{epic_context}

Break these requirements into user stories following the stack's
planning guidance above. Each story should be independently shippable
and small enough for one developer's day. Consider the repository
context and ensure stories are actionable for developers."""

        response = await openai.generate(
            prompt=prompt,
            system=STORIES_SYSTEM_PROMPT,
            response_format={"type": "json_object"},
        )

        try:
            stories_data = json.loads(response)
            if isinstance(stories_data, dict) and "stories" in stories_data:
                stories_data = stories_data["stories"]
        except (json.JSONDecodeError, ValueError) as e:
            # Raise instead of swallowing: a stage that "completes" with zero
            # stories is invisible breakage. Raising lets the executor's
            # transient retry re-ask the LLM, and a persistent failure shows
            # up as a FAILED stage instead of an empty success.
            raise ValueError(f"stories response unparseable: {e}; raw[:200]={response[:200]!r}") from e

        # Create GitHub issues for each story
        created_stories: list[dict[str, Any]] = []
        story_numbers: list[int] = []

        for story in stories_data:
            if isinstance(story, str):
                story = {"title": story[:100], "description": story, "acceptance_criteria": [], "priority": "medium"}
            if not isinstance(story, dict):
                continue
            ac = story.get("acceptance_criteria", [])
            body = f"{story.get('description', '')}\n\n## Acceptance Criteria\n"
            for criterion in ac:
                body += f"\n- [ ] {criterion}"
            body += f"\n\n**Priority:** {story.get('priority', 'medium')}"
            if epic_number:
                body += f"\n\n**Epic:** #{epic_number}"

            labels = [lbl for lbl in story.get("labels", []) if isinstance(lbl, str)] + [
                "devai:user-story",
                f"priority:{story.get('priority', 'medium')}",
            ]
            run_id = str(state.get("run_id") or "")
            if run_id:
                labels.append(f"devai:run:{run_id.removeprefix('devai-')[:10]}")
            story_title = (story.get("title") or "").strip() or "User Story"

            issue = await self._create_issue_safe(
                repo=repo,
                title=story_title,
                body=body,
                labels=labels,
                dedupe_labels=["devai:user-story"],
            )
            created_stories.append(
                {
                    "title": story.get("title", ""),
                    "description": story.get("description", ""),
                    "priority": story.get("priority", "medium"),
                    "number": issue["number"],
                    "url": issue["html_url"],
                    "acceptance_criteria": ac,
                }
            )
            story_numbers.append(issue["number"])

            # Add story to the Supervisor's project board
            project_id = state.get("supervisor_project_id")
            if project_id:
                await self._add_to_project(repo, project_id, issue["number"])

        # Link every story to the epic. The task-list goes in the epic BODY
        # — GitHub only builds the tracked-issues relationship (progress bar,
        # "Tracked by" backlinks on each story) from body task-lists, not
        # comments. The comment stays as the human-readable announcement.
        if epic_number and created_stories:
            story_refs = "\n".join(f"- [ ] #{s['number']} — {s['title']}" for s in created_stories)
            try:
                epic_issue = await self.github.get_issue(repo, epic_number)
                current_body = epic_issue.get("body") or ""
                if "## User Stories" not in current_body:
                    await self.github.update_issue(
                        repo,
                        epic_number,
                        body=f"{current_body}\n\n## User Stories\n\n{story_refs}",
                    )
            except Exception:  # noqa: BLE001 — linking is best-effort, never fail the stage
                logger.exception("epic body task-list update failed for #%s", epic_number)
            await self.github.add_comment(
                repo,
                epic_number,
                f"## User Stories\n\n{story_refs}\n\n"
                f"_Each story is tracked on this epic — progress updates will be posted here as the pipeline executes._",
            )

        # Handoff to Engineering Manager
        a2a.handoff(
            "engineering_manager",
            "Stories Ready for Planning",
            f"Created {len(created_stories)} user stories from epic #{epic_number}.\n"
            + "\n".join(f"- #{s['number']}: {s['title']}" for s in created_stories[:10]),
            payload={"story_count": len(created_stories)},
        )

        return {
            "stories": created_stories,
            "story_issue_numbers": story_numbers,
            # a2a messages are merged by BaseAgent.run() automatically
        }

    async def _create_issue_safe(
        self,
        repo: str,
        title: str,
        body: str,
        labels: list[str] | None = None,
        dedupe_labels: list[str] | None = None,
    ) -> dict[str, Any]:
        """Idempotent issue creation with a safe fallback.

        Calls ``create_issue_idempotent`` if the SCM client exposes it
        (the multi-SCM ``GitHubSCMClient`` and the legacy
        ``core.github_client.GitHubClient`` both do as of the dedup
        rollout). Falls back to ``create_issue`` for any other client
        that doesn't have the helper, so the agent never crashes the
        run with ``AttributeError`` over a missing dedup helper.
        """
        if hasattr(self.github, "create_issue_idempotent"):
            return await self.github.create_issue_idempotent(
                repo=repo,
                title=title,
                body=body,
                labels=labels,
                dedupe_labels=dedupe_labels,
            )
        logger.warning(
            "SCM client %s has no create_issue_idempotent — falling back "
            "to plain create_issue (this run will not dedup)",
            type(self.github).__name__,
        )
        return await self.github.create_issue(
            repo=repo,
            title=title,
            body=body,
            labels=labels,
        )

    async def _add_to_project(self, repo: str, project_id: str, issue_number: int) -> None:
        """Add an issue to the Supervisor's project board."""
        try:
            node_id = await self.scm.get_node_id(repo, issue_number)
            if node_id:
                await self.scm.add_item_to_project(project_id, node_id)
                logger.debug("Added issue #%d to project board", issue_number)
        except Exception as e:
            logger.warning("Failed to add issue #%d to project: %s", issue_number, e)
