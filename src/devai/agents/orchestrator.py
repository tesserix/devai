"""Orchestrator Agent — manages execution workflow and dynamic routing.

The Orchestrator sits between the Supervisor (planning) and specialist agents (execution).
It:
1. Receives the delegation plan from the Supervisor
2. Manages the execution workflow: code -> review -> test -> fix -> deploy
3. Makes dynamic routing decisions based on agent outputs
4. Posts progress updates to the SCM tracking issue
5. Handles failure recovery, retries, and iteration loops
6. Tracks progress and reports status back to the Supervisor

Uses OpenAI for intelligent routing decisions that go beyond simple conditionals.
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

SYSTEM_PROMPT = """You are the Orchestrator Agent — the execution coordinator for a collaborative AI-powered software development pipeline.

Your role is to manage the real-time execution of the development workflow, making intelligent routing decisions at each checkpoint.

## Collaborative Parallel Model

This pipeline processes stories INDIVIDUALLY on separate feature branches:
- Each story goes through: implement → review → security → test
- After all stories pass quality gates, the Release Manager merges all PRs
- You coordinate the per-story quality gates and track overall progress

## Your Responsibilities

1. **Story-Level Routing**: At each quality gate for the active story, decide:
   - After code review: approve → security scan, or request changes → re-implement
   - After security scan: pass → test, block → re-implement
   - After tests: pass → mark story done, fail → re-implement
   - After any failure: retry, escalate to supervisor, or abort

2. **Progress Tracking**: Monitor execution and report:
   - Which story is currently being processed (and which are done)
   - Current quality gate for the active story
   - Agent performance (timing, quality)
   - Blockers and risks
   - Iteration count for feedback loops

3. **Conflict Resolution**: When agents produce conflicting outputs:
   - Analyze the conflict
   - Decide which approach to follow
   - Provide clear guidance for resolution

## Decision Context

You will receive the current pipeline state including:
- The Supervisor's plan and guidance
- Active story index and story branch statuses
- Results from completed agents
- A2A messages between agents
- Review feedback, security findings, test results

## Output Format

Return your routing decision as JSON:

```json
{
  "current_phase": "Name of the current phase",
  "current_step": "Name of the current step",
  "active_story": "Story #<number> — <title>",
  "decision": "continue|loop_back|escalate|abort",
  "next_agent": "agent_name to execute next (if continue)",
  "loop_target": "agent_name to loop back to (if loop_back)",
  "reason": "Why this decision was made",
  "guidance_for_next": "Specific instructions for the next agent",
  "iteration_count": 0,
  "max_iterations_remaining": 3,
  "risk_level": "low|medium|high|critical",
  "progress_pct": 45,
  "status_summary": "Brief status update for the dashboard"
}
```

Be decisive. When quality gates fail, provide specific feedback about what needs fixing."""


class OrchestratorAgent(BaseAgent):
    """Execution workflow coordinator with dynamic routing intelligence.

    Manages the code -> review -> test -> fix -> deploy cycle,
    making LLM-powered routing decisions at each checkpoint.
    Posts progress updates to the SCM tracking issue.
    """

    name = "orchestrator"

    async def _execute_graph(self, state: ALMState, a2a: A2ABus) -> dict[str, Any]:
        """Evaluate current pipeline state and make a routing decision."""
        openai = OpenAIProvider(self.config)

        # Gather execution context
        stage = state.get("stage", "triggered")
        review_decision = state.get("review_decision", "")
        security_decision = state.get("security_decision", "")
        test_failed = state.get("test_failed", 0)
        review_iteration = state.get("review_iteration", 0)
        supervisor_plan = state.get("supervisor_plan", {})
        agent_timings = state.get("agent_timings", {})

        # Story-level context
        active_idx = state.get("active_story_index", 0)
        stories = state.get("stories", [])
        story_branches = state.get("story_branches", [])
        active_story = stories[active_idx] if active_idx < len(stories) else {}
        story_number = active_story.get("number", "?")
        story_title = active_story.get("title", "")

        # Story progress summary
        story_status_lines = []
        for sb in story_branches:
            status = sb.get("status", "pending")
            s_num = sb.get("story_number", "?")
            s_title = sb.get("story_title", "")[:30]
            icon = {"pending": "⏳", "implementing": "🔧", "approved": "✅", "merged": "🚀", "failed": "❌"}.get(
                status, "❓"
            )
            story_status_lines.append(f"  {icon} Story #{s_num}: {s_title} — {status}")
        story_progress = "\n".join(story_status_lines) if story_status_lines else "  (no stories)"

        # Build A2A context
        inbox_context = a2a.format_inbox_context()

        context_parts = [
            "## Current Pipeline State",
            f"- Stage: {stage}",
            f"- Active Story: #{story_number} — {story_title}",
            f"- Review Decision: {review_decision or 'N/A'}",
            f"- Security Decision: {security_decision or 'N/A'}",
            f"- Test Failures: {test_failed}",
            f"- Review Iteration: {review_iteration}",
            f"- Completed Agents: {', '.join(agent_timings.keys()) or 'None'}",
            f"\n## Story Progress\n{story_progress}",
        ]

        if supervisor_plan:
            plan_summary = supervisor_plan.get("project_summary", "")
            phases = supervisor_plan.get("delegation_plan", {}).get("phases", [])
            context_parts.append(f"\n## Supervisor Plan\n{plan_summary}")
            context_parts.append(f"Phases: {json.dumps([p.get('name', '') for p in phases])}")

        # Include review feedback if any
        review_feedback = state.get("review_feedback", [])
        if review_feedback:
            context_parts.append("\n## Review Feedback\n" + "\n".join(f"- {fb}" for fb in review_feedback[-3:]))

        # Include security findings if any
        security_findings = state.get("security_findings", [])
        if security_findings:
            findings_text = "\n".join(
                f"- [{f.get('severity', 'medium')}] {f.get('title', '')}" for f in security_findings[:5]
            )
            context_parts.append(f"\n## Security Findings\n{findings_text}")

        # Include test failures if any
        test_failures = state.get("test_failures", [])
        if test_failures:
            failures_text = "\n".join(f"- {f.get('test', '')}: {f.get('error', '')[:100]}" for f in test_failures[:5])
            context_parts.append(f"\n## Test Failures\n{failures_text}")

        if inbox_context:
            context_parts.append(f"\n{inbox_context}")

        memory_context = state.get("memory_context", "")
        if memory_context:
            context_parts.append(f"\n## Relevant Memory From Past Runs\n{memory_context}")

        user_message = "\n".join(context_parts)
        user_message += "\n\nBased on the current state, what should happen next? Provide your routing decision."

        decision_text = await openai.generate(
            prompt=user_message,
            system=SYSTEM_PROMPT,
        )

        # Parse the decision
        routing = self._extract_decision(decision_text, state)

        # Calculate progress: planning (30%) + stories (50%) + deploy (20%)
        total_stories = max(len(story_branches), 1)
        done_stories = sum(1 for sb in story_branches if sb.get("status") in ("approved", "merged", "failed"))
        planning_done = 1 if stage not in ("triggered", "plan_approved") else 0
        deploy_done = 1 if stage in ("deployed", "done") else 0

        progress = int(planning_done * 30 + (done_stories / total_stories) * 50 + deploy_done * 20)
        routing["progress_pct"] = min(progress, 100)

        # Post progress update to the SCM tracking issue
        await self._post_progress_update(state, routing)

        # Send status update via A2A
        a2a.notify(
            "supervisor",
            f"Pipeline Progress: {routing['progress_pct']}%",
            f"Phase: {routing.get('current_phase', stage)}\n"
            f"Decision: {routing.get('decision', 'continue')}\n"
            f"Status: {routing.get('status_summary', '')}",
            payload={"routing": routing},
        )

        # If looping back, notify the target agent with specific guidance
        if routing.get("decision") == "loop_back" and routing.get("loop_target"):
            guidance = routing.get("guidance_for_next", "Please address the feedback and try again.")
            a2a.handoff(
                routing["loop_target"],
                f"Revision Required (iteration {review_iteration + 1})",
                guidance,
                payload={
                    "iteration": review_iteration + 1,
                    "reason": routing.get("reason", ""),
                },
            )

        # If escalating, notify supervisor
        if routing.get("decision") == "escalate":
            a2a.escalate(
                "supervisor",
                f"Escalation: {routing.get('reason', 'Unknown issue')}",
                f"The pipeline has hit a blocker at stage '{stage}'.\n"
                f"Risk level: {routing.get('risk_level', 'high')}\n"
                f"Details: {routing.get('reason', '')}",
            )

        return {
            "orchestrator_routing": routing,
            "orchestrator_decision_raw": decision_text,
        }

    async def _post_progress_update(self, state: ALMState, routing: dict[str, Any]) -> None:
        """Post a progress update comment to the SCM tracking issue."""
        repo = state.get("repo_full_name", "")
        tracking_issue = state.get("supervisor_tracking_issue")
        if not repo or not tracking_issue:
            return

        stage = state.get("stage", "unknown")
        progress = routing.get("progress_pct", 0)
        decision = routing.get("decision", "continue")
        phase = routing.get("current_phase", "")
        status = routing.get("status_summary", "")
        agent_timings = state.get("agent_timings", {})

        # Build a concise progress bar
        filled = int(progress / 5)
        bar = "█" * filled + "░" * (20 - filled)

        # Build timing summary for completed agents
        timing_lines = ""
        if agent_timings:
            timing_lines = "\n".join(
                f"| {agent} | {dur:.1f}s | :white_check_mark: |" for agent, dur in agent_timings.items()
            )
            timing_lines = f"\n| Agent | Duration | Status |\n|---|---|---|\n{timing_lines}\n"

        body = (
            f"### Orchestrator Update — {phase}\n\n"
            f"**Progress:** `{bar}` {progress}%\n"
            f"**Stage:** `{stage}`\n"
            f"**Decision:** `{decision}`\n"
        )

        if status:
            body += f"**Status:** {status}\n"

        if decision == "loop_back":
            body += f"\n:arrows_counterclockwise: **Looping back** — {routing.get('reason', '')}\n"
        elif decision == "escalate":
            body += f"\n:warning: **Escalated** — {routing.get('reason', '')}\n"

        if timing_lines:
            body += f"\n{timing_lines}"

        try:
            await self.scm.add_comment(repo, tracking_issue, body)
        except Exception as e:
            logger.warning("Failed to post progress update to #%s: %s", tracking_issue, e)

    def _extract_decision(self, text: str, state: ALMState) -> dict[str, Any]:
        """Extract structured routing decision from the LLM response."""
        try:
            start = text.index("{")
            end = text.rindex("}") + 1
            return json.loads(text[start:end])
        except (ValueError, json.JSONDecodeError):
            pass

        # Fallback: infer decision from current state
        stage = state.get("stage", "triggered")
        review_decision = state.get("review_decision", "")
        security_decision = state.get("security_decision", "")
        test_failed = state.get("test_failed", 0)
        iteration = state.get("review_iteration", 0)

        decision = "continue"
        reason = "Proceeding with normal flow"

        if review_decision == "changes_requested" and iteration < 3:
            decision = "loop_back"
            reason = "Code review requested changes"
        elif security_decision == "block":
            decision = "loop_back"
            reason = "Security scan blocked — vulnerabilities must be fixed"
        elif test_failed > 0 and iteration < 5:
            decision = "loop_back"
            reason = f"{test_failed} test(s) failed — fixing required"
        elif iteration >= 5:
            decision = "escalate"
            reason = f"Max iterations ({iteration}) reached without resolution"

        return {
            "current_phase": self._stage_to_phase(stage),
            "current_step": stage,
            "decision": decision,
            "reason": reason,
            "risk_level": "high" if decision == "escalate" else "medium" if decision == "loop_back" else "low",
            "status_summary": f"Stage: {stage}, Decision: {decision}",
            "iteration_count": iteration,
        }

    def _stage_to_phase(self, stage: str) -> str:
        """Map a pipeline stage to its phase name."""
        phase_map = {
            "triggered": "Analysis",
            "requirements_analyzed": "Analysis",
            "epic_created": "Planning",
            "stories_created": "Planning",
            "plan_created": "Planning",
            "plan_approved": "Planning",
            "code_implemented": "Implementation",
            "code_reviewed": "Quality",
            "security_cleared": "Quality",
            "build_monitoring": "Quality",
            "tests_complete": "Quality",
            "deploying": "Deployment",
            "deployed": "Deployment",
        }
        return phase_map.get(stage, "Unknown")
