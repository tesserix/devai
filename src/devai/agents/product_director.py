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
from devai.providers.openai_codex import CodexLiteProvider

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
    """Creates Epics and User Stories using OpenAI Codex Responses API."""

    name = "product_director"
    subscribe_subject = "devai.pipeline.trigger"
    publish_subject = "devai.pipeline.stories_ready"

    async def _execute_graph(self, state: ALMState, a2a: A2ABus) -> dict[str, Any]:
        """Default execution — creates stories, passing A2A bus through."""
        return await self.run_stories(state, a2a)

    async def run_epic(self, state: ALMState, a2a: A2ABus | None = None) -> dict[str, Any]:
        """Create a GitHub Epic from analyzed requirements."""
        codex = CodexLiteProvider(self.config)
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

        prompt = f"""Repository: {repo}

## Analyzed Requirements
{req_summary}

## Raw Requirements
{requirements[:2000]}

{inbox_context}

Create a GitHub Epic that encompasses all these requirements."""

        response = await codex.generate(
            prompt=prompt,
            system=EPIC_SYSTEM_PROMPT,
            response_format={"type": "json_object"},
        )

        try:
            epic_data = json.loads(response)
        except json.JSONDecodeError:
            epic_data = {"title": "Feature Epic", "description": requirements[:500], "labels": ["epic"]}

        # Create the Epic as a GitHub issue
        labels = epic_data.get("labels", []) + ["epic", "devai:epic"]
        body = f"{epic_data.get('description', '')}\n\n## Milestones\n"
        for m in epic_data.get("milestones", []):
            body += f"\n- [ ] {m}"

        issue = await self.github.create_issue(
            repo=repo,
            title=epic_data.get("title", "Feature Epic"),
            body=body,
            labels=labels,
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
        codex = CodexLiteProvider(self.config)
        if a2a is None:
            a2a = A2ABus(self.name, state.get("a2a_messages", []))

        repo = state.get("repo_full_name", "")
        requirements = state.get("requirements", "")
        analyzed = state.get("analyzed_requirements", [])
        epics = state.get("epics", [])
        epic_number = state.get("epic_issue_number")

        req_summary = ""
        if analyzed:
            req_summary = "\n".join(
                f"- [{r.get('priority', 'medium')}] {r.get('title', '')}: {r.get('description', '')[:150]}"
                for r in analyzed[:15]
            )
        else:
            req_summary = requirements

        epic_context = ""
        if epics:
            epic_context = f"\n## Parent Epic\n{epics[0].get('title', '')}\n{epics[0].get('description', '')[:300]}"

        prompt = f"""Repository: {repo}

## Requirements
{req_summary}
{epic_context}

Break these requirements into user stories. Consider the repository context and ensure stories are actionable for developers."""

        response = await codex.generate(
            prompt=prompt,
            system=STORIES_SYSTEM_PROMPT,
            response_format={"type": "json_object"},
        )

        try:
            stories_data = json.loads(response)
            if isinstance(stories_data, dict) and "stories" in stories_data:
                stories_data = stories_data["stories"]
        except (json.JSONDecodeError, ValueError) as e:
            logger.error("Failed to parse stories: %s", e)
            stories_data = []

        # Create GitHub issues for each story
        created_stories: list[dict[str, Any]] = []
        story_numbers: list[int] = []

        for story in stories_data:
            ac = story.get("acceptance_criteria", [])
            body = f"{story.get('description', '')}\n\n## Acceptance Criteria\n"
            for criterion in ac:
                body += f"\n- [ ] {criterion}"
            body += f"\n\n**Priority:** {story.get('priority', 'medium')}"
            if epic_number:
                body += f"\n\n**Epic:** #{epic_number}"

            labels = story.get("labels", []) + ["devai:user-story", f"priority:{story.get('priority', 'medium')}"]

            issue = await self.github.create_issue(
                repo=repo,
                title=story.get("title", "User Story"),
                body=body,
                labels=labels,
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

        # Update epic body with story references
        if epic_number and created_stories:
            story_refs = "\n".join(f"- [ ] #{s['number']} — {s['title']}" for s in created_stories)
            await self.github.add_comment(
                repo,
                epic_number,
                f"## User Stories\n\n{story_refs}",
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

    async def _add_to_project(self, repo: str, project_id: str, issue_number: int) -> None:
        """Add an issue to the Supervisor's project board."""
        try:
            node_id = await self.scm.get_node_id(repo, issue_number)
            if node_id:
                await self.scm.add_item_to_project(project_id, node_id)
                logger.debug("Added issue #%d to project board", issue_number)
        except Exception as e:
            logger.warning("Failed to add issue #%d to project: %s", issue_number, e)
