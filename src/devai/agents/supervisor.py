"""Supervisor Agent — top-level planning and delegation coordinator.

The Supervisor is the first agent to receive a user request. It:
1. Analyzes the request and decomposes it into a high-level architecture plan
2. Identifies which specialist agents are needed and in what order
3. Creates a delegation plan with dependencies between agents
4. Creates a tracking issue/work item on the SCM provider with the plan
5. Monitors agent outputs via A2A messages and adjusts the plan dynamically
6. Escalates blockers and resolves conflicts between agents

Uses OpenAI for maximum reasoning capability on architectural decisions.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from devai.core.base_agent import BaseAgent

# Primary: OpenAI | Fallback: Claude
from devai.providers.openai_provider import OpenAIProvider

# Groq available as fallback: from devai.providers.groq_provider import GroqProvider

if TYPE_CHECKING:
    from devai.graph.a2a import A2ABus
    from devai.graph.state import ALMState

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are the Supervisor Agent — the senior technical architect and team lead for an AI-powered software development pipeline.

Your role is to receive a user request and create a comprehensive execution plan that coordinates multiple specialist agents to deliver production-ready software.

## Collaborative Parallel Model

This pipeline uses a **collaborative parallel model** — NOT a sequential waterfall:
- User stories are implemented on SEPARATE feature branches (story/<number>-<slug>)
- Each story goes through its own quality cycle: implement → review → security → test
- Agents collaborate via A2A messaging throughout the process
- The Engineering Manager creates per-story technical plans with file ownership
- The Release Manager merges all approved story PRs to main after all pass quality gates
- You are the Engineering Director overseeing this collaborative team

## Your Responsibilities

1. **Analyze the Request**: Understand what the user wants built, the scope, constraints, and quality requirements.

2. **Architecture Planning**: Design the high-level technical architecture including:
   - System components and their interactions
   - Technology stack recommendations (languages, frameworks, databases)
   - API design patterns and data models
   - Security considerations and deployment strategy
   - Performance and scalability requirements

3. **Story Decomposition Guidance**: Guide the Product Director on how to break the epic into stories:
   - Stories should be independently implementable (minimal file overlap)
   - Each story should be small enough for 1-3 days of work
   - Stories should be mergeable in any order without conflicts
   - Identify shared infrastructure changes that should be a separate story

4. **Delegation Planning**: Create a structured plan specifying:
   - Which specialist agents to involve and why
   - Quality gates and checkpoints per story
   - Risk areas that need extra attention

5. **Conflict Resolution**: When agents disagree (e.g., reviewer rejects developer's code), you:
   - Analyze the feedback from both sides
   - Make a decision on the right approach
   - Provide clear guidance for the next iteration

## Available Specialist Agents

| Agent | Role | Operates Per-Story? |
|-------|------|---------------------|
| Document Analyzer | Extract requirements from docs/URLs | No — global |
| Tech Detector | Auto-detect existing tech stack | No — global |
| Requirements Analyst | Refine and validate requirements | No — global |
| Product Director | Create epics and user stories | No — creates all stories |
| Engineering Manager | Create per-story technical plans | No — plans all stories |
| Senior Developer | Write production code on feature branch | YES — per story |
| DB Engineer | Design and review database schemas | YES — per story |
| Staff Reviewer | Code review with quality standards | YES — per story |
| Security Expert | Security scanning and vulnerability detection | YES — per story |
| QA Tester | Run tests and validate quality | YES — per story |
| CI Monitor | Monitor CI/CD builds | After all stories merged |
| Infra Provisioner | Generate Helm charts and deploy configs | After all stories merged |
| Release Manager | Merge story PRs + deploy | Merge all → deploy |

## Output Format

Return your plan as a structured JSON document:

```json
{
  "project_summary": "Brief description of what we're building",
  "architecture": {
    "overview": "High-level architecture description",
    "components": ["list of system components"],
    "tech_stack": {"frontend": "...", "backend": "...", "database": "...", "infra": "..."},
    "patterns": ["design patterns to use"],
    "risks": ["identified technical risks"]
  },
  "delegation_plan": {
    "phases": [
      {
        "name": "Phase name",
        "agents": ["agent_name"],
        "objective": "What this phase achieves",
        "dependencies": ["previous phase name"],
        "quality_gates": ["what must pass before moving on"],
        "estimated_complexity": "low|medium|high"
      }
    ]
  },
  "quality_requirements": {
    "code_coverage_target": "80%",
    "security_level": "standard|strict|critical",
    "review_focus_areas": ["areas needing extra review attention"],
    "testing_strategy": "unit|integration|e2e|all"
  },
  "guidance_for_agents": {
    "agent_name": "Specific instructions for this agent"
  }
}
```

## Important Operational Constraints

1. **Private Repo Build Limits**: The tesserix GitHub org has limited Actions minutes for private repos.
   The CI Monitor agent handles this automatically via a public→build→private cycle.

2. **ArgoCD-Only Deployments**: Never use kubectl apply directly. All K8s changes go through ArgoCD via the tesserix-k8s repo.

3. **Database Schemas**: Never create SQL files in app repos. All schemas live in tesserix-k8s/charts/apps/db-schema-bootstrap/schemas/.

Be thorough but practical. Focus on what will produce the best production-ready result."""


class SupervisorAgent(BaseAgent):
    """Top-level planning and delegation coordinator.

    Analyzes user requests, plans architecture, creates delegation plans,
    creates tracking issues on the SCM provider, and coordinates specialist
    agents via A2A messaging.
    """

    name = "supervisor"

    async def _execute_graph(self, state: ALMState, a2a: A2ABus) -> dict[str, Any]:
        """Analyze the request and create a comprehensive execution plan."""
        openai = OpenAIProvider(self.config)

        repo = state.get("repo_full_name", "")
        requirements = state.get("requirements", "")
        analyzed_reqs = state.get("analyzed_requirements", [])
        tech_stack = state.get("detected_tech_stack", "")

        # Check for escalations from previous iterations
        escalations = a2a.get_escalations()
        review_feedback = state.get("review_feedback", [])
        inbox_context = a2a.format_inbox_context()

        # Build context from what we know so far
        context_parts = [f"## Repository\n{repo}"]
        context_parts.append(f"## User Request\n{requirements}")

        if analyzed_reqs:
            req_summary = "\n".join(
                f"- [{r.get('category', 'functional')}] {r.get('title', '')}: {r.get('description', '')[:100]}"
                for r in analyzed_reqs[:15]
            )
            context_parts.append(f"## Analyzed Requirements\n{req_summary}")

        if tech_stack:
            context_parts.append(f"## Detected Tech Stack\n{tech_stack}")

        memory_context = state.get("memory_context", "")
        if memory_context:
            context_parts.append(f"## Relevant Memory From Past Runs\n{memory_context}")

        if review_feedback:
            context_parts.append(
                "## Review Feedback from Previous Iteration\n" + "\n".join(f"- {fb}" for fb in review_feedback)
            )

        if inbox_context:
            context_parts.append(inbox_context)

        # Add governance context
        governance = state.get("governance", "")
        system = SYSTEM_PROMPT
        if governance:
            system += f"\n\n## Repository Governance (CLAUDE.md)\nYou MUST follow these rules:\n\n{governance}"

        user_message = "\n\n".join(context_parts)

        if escalations:
            escalation_text = "\n".join(f"- **{e['from_agent']}**: {e['subject']} — {e['body']}" for e in escalations)
            user_message += (
                f"\n\n## Agent Escalations Requiring Your Decision\n{escalation_text}"
                "\n\nPlease address these escalations in your revised plan."
            )

        plan_text = await openai.generate(
            prompt=user_message,
            system=system,
        )

        # Parse the plan (extract JSON if present)
        supervisor_plan = self._extract_plan(plan_text)

        # Create a tracking issue on the SCM provider with the plan
        tracking_issue = await self._create_tracking_issue(repo, supervisor_plan, plan_text)

        # Create a GitHub Project v2 board for this pipeline run
        project = await self._create_project_board(repo, supervisor_plan, tracking_issue)

        # Add the tracking issue to the project board
        if project.get("id") and tracking_issue.get("number"):
            await self._add_issue_to_project(repo, project["id"], tracking_issue["number"])

        # Broadcast the plan to all agents via A2A
        a2a.broadcast(
            "Supervisor Plan Published",
            f"Architecture plan created for: {supervisor_plan.get('project_summary', repo)}\n\n"
            f"Phases: {len(supervisor_plan.get('delegation_plan', {}).get('phases', []))}\n"
            f"Tracking issue: #{tracking_issue.get('number', '?')}\n"
            f"Project board: {project.get('url', 'N/A')}\n"
            f"Tech stack: {json.dumps(supervisor_plan.get('architecture', {}).get('tech_stack', {}), indent=2)}",
        )

        # Send targeted guidance to key agents
        guidance = supervisor_plan.get("guidance_for_agents", {})
        for agent_name, instructions in guidance.items():
            a2a.handoff(
                agent_name,
                f"Supervisor Guidance for {agent_name}",
                instructions,
                payload={
                    "from_plan": True,
                    "tracking_issue": tracking_issue.get("number"),
                    "project_id": project.get("id"),
                },
            )

        # Notify orchestrator about the execution plan
        a2a.handoff(
            "orchestrator",
            "Execution Plan Ready",
            f"Delegation plan with {len(supervisor_plan.get('delegation_plan', {}).get('phases', []))} phases is ready. "
            f"Tracking issue: #{tracking_issue.get('number', '?')}. "
            f"Project board: {project.get('url', 'N/A')}. "
            "Proceed with execution according to the defined phase order.",
            payload={
                "delegation_plan": supervisor_plan.get("delegation_plan", {}),
                "tracking_issue_number": tracking_issue.get("number"),
                "project_id": project.get("id"),
                "project_url": project.get("url"),
            },
        )

        return {
            "supervisor_plan": supervisor_plan,
            "supervisor_plan_raw": plan_text,
            "supervisor_tracking_issue": tracking_issue.get("number"),
            "supervisor_project_id": project.get("id"),
            "supervisor_project_url": project.get("url"),
            "stage": "plan_approved",
        }

    async def _create_tracking_issue(
        self,
        repo: str,
        plan: dict[str, Any],
        raw_plan: str,
    ) -> dict[str, Any]:
        """Create a tracking issue on the SCM provider with the execution plan."""
        project_summary = plan.get("project_summary", "DevAI Pipeline Execution")
        phases = plan.get("delegation_plan", {}).get("phases", [])
        architecture = plan.get("architecture", {})
        quality = plan.get("quality_requirements", {})

        # Build the issue body
        body_parts = [
            f"## Supervisor Plan\n\n{project_summary}\n",
        ]

        # Architecture section
        if architecture:
            body_parts.append("### Architecture\n")
            if architecture.get("overview"):
                body_parts.append(architecture["overview"][:500])
            tech_stack = architecture.get("tech_stack", {})
            if tech_stack:
                body_parts.append("\n**Tech Stack:**")
                for layer, tech in tech_stack.items():
                    body_parts.append(f"- **{layer}:** {tech}")

        # Phase checklist
        if phases:
            body_parts.append("\n### Execution Phases\n")
            for phase in phases:
                agents = ", ".join(phase.get("agents", []))
                complexity = phase.get("estimated_complexity", "medium")
                body_parts.append(
                    f"- [ ] **{phase.get('name', 'Phase')}** ({complexity})\n"
                    f"  - Agents: {agents}\n"
                    f"  - Objective: {phase.get('objective', '')}"
                )

        # Quality gates
        if quality:
            body_parts.append("\n### Quality Requirements\n")
            if quality.get("code_coverage_target"):
                body_parts.append(f"- Coverage target: {quality['code_coverage_target']}")
            if quality.get("security_level"):
                body_parts.append(f"- Security level: {quality['security_level']}")
            if quality.get("testing_strategy"):
                body_parts.append(f"- Testing strategy: {quality['testing_strategy']}")

        body_parts.append(
            "\n---\n*This issue tracks the DevAI pipeline execution. "
            "Progress updates will be posted as each phase completes.*"
        )

        body = "\n".join(body_parts)

        try:
            # Defensive: fall back to plain create_issue if the SCM
            # client doesn't have the dedup helper.
            if hasattr(self.scm, "create_issue_idempotent"):
                issue = await self.scm.create_issue_idempotent(
                    repo=repo,
                    title=f"[DevAI] {project_summary[:80]}",
                    body=body,
                    labels=["devai:tracking", "devai:supervisor-plan"],
                    dedupe_labels=["devai:supervisor-plan"],
                )
            else:
                logger.warning(
                    "SCM client %s missing create_issue_idempotent — using plain create_issue",
                    type(self.scm).__name__,
                )
                issue = await self.scm.create_issue(
                    repo=repo,
                    title=f"[DevAI] {project_summary[:80]}",
                    body=body,
                    labels=["devai:tracking", "devai:supervisor-plan"],
                )
            logger.info(
                "Created tracking issue #%s on %s",
                issue.get("number", "?"),
                repo,
            )
            return issue
        except Exception as e:
            logger.warning("Failed to create tracking issue on %s: %s", repo, e)
            return {"number": None, "html_url": ""}

    async def _create_project_board(
        self,
        repo: str,
        plan: dict[str, Any],
        tracking_issue: dict[str, Any],
    ) -> dict[str, Any]:
        """Create a GitHub Project v2 board for this pipeline run."""
        org = repo.split("/")[0] if "/" in repo else self.config.github_org
        project_summary = plan.get("project_summary", "DevAI Pipeline")
        phases = plan.get("delegation_plan", {}).get("phases", [])

        # Build project description
        description = f"## {project_summary}\n\nAutomated ALM pipeline managed by DevAI.\n\n**Phases:** {len(phases)}\n"
        for phase in phases:
            description += f"- **{phase.get('name', '')}**: {phase.get('objective', '')}\n"

        if tracking_issue.get("number"):
            description += f"\n**Tracking Issue:** #{tracking_issue['number']}\n"

        try:
            project = await self.scm.create_project(
                org=org,
                title=f"DevAI: {project_summary[:60]}",
                description=description,
            )
            logger.info(
                "Created project board '%s' on %s: %s",
                project.get("title", "?"),
                org,
                project.get("url", ""),
            )

            # Post project link on the tracking issue
            if tracking_issue.get("number") and project.get("url"):
                await self.scm.add_comment(
                    repo,
                    tracking_issue["number"],
                    f"**Project Board Created**\n\n"
                    f"All epics, stories, and tasks will be tracked on the project board:\n"
                    f"{project['url']}",
                )

            return project
        except Exception as e:
            logger.warning("Failed to create project board on %s: %s", org, e)
            return {"id": None, "url": ""}

    async def _add_issue_to_project(
        self,
        repo: str,
        project_id: str,
        issue_number: int,
    ) -> None:
        """Add an issue to the project board."""
        try:
            node_id = await self.scm.get_node_id(repo, issue_number)
            if node_id:
                await self.scm.add_item_to_project(project_id, node_id)
                logger.debug("Added issue #%d to project %s", issue_number, project_id)
        except Exception as e:
            logger.warning("Failed to add issue #%d to project: %s", issue_number, e)

    def _extract_plan(self, text: str) -> dict[str, Any]:
        """Extract structured plan from the LLM response."""
        # Try to find JSON in the response
        try:
            start = text.index("{")
            end = text.rindex("}") + 1
            return json.loads(text[start:end])
        except (ValueError, json.JSONDecodeError):
            pass

        # Fallback: create a structured plan from the text
        return {
            "project_summary": text[:200],
            "architecture": {"overview": text},
            "delegation_plan": {
                "phases": [
                    {
                        "name": "Analysis",
                        "agents": ["document_analyzer", "tech_detector", "requirements_analyst"],
                        "objective": "Understand the codebase and requirements",
                    },
                    {
                        "name": "Planning",
                        "agents": ["product_director", "engineering_manager"],
                        "objective": "Create epics, stories, and technical plan",
                    },
                    {
                        "name": "Implementation",
                        "agents": ["senior_developer", "db_engineer"],
                        "objective": "Write code and handle database changes",
                    },
                    {
                        "name": "Quality",
                        "agents": ["staff_reviewer", "security_expert", "qa_tester"],
                        "objective": "Review, scan, and test",
                    },
                    {
                        "name": "Deployment",
                        "agents": ["ci_monitor", "infra_provisioner", "release_manager"],
                        "objective": "Build, provision, and deploy",
                    },
                ]
            },
            "quality_requirements": {},
            "guidance_for_agents": {},
        }
