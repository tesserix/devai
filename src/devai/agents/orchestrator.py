"""Orchestrator Agent — manages execution workflow and dynamic routing.

The Orchestrator sits between the Supervisor (planning) and specialist agents (execution).
It:
1. Receives the delegation plan from the Supervisor
2. Manages the execution workflow: code -> review -> test -> fix -> deploy
3. Makes dynamic routing decisions based on agent outputs
4. Posts progress updates to the SCM tracking issue
5. Handles failure recovery, retries, and iteration loops
6. Tracks progress and reports status back to the Supervisor

Uses Claude for intelligent routing decisions that go beyond simple conditionals.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from devai.core.base_agent import BaseAgent
from devai.providers.anthropic_claude import ClaudeProvider

if TYPE_CHECKING:
    from devai.graph.a2a import A2ABus
    from devai.graph.state import ALMState

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are the Orchestrator Agent — the execution coordinator for an AI-powered software development pipeline.

Your role is to manage the real-time execution of the development workflow, making intelligent routing decisions at each checkpoint.

## Your Responsibilities

1. **Workflow Management**: Execute the pipeline phases in order:
   - Analysis Phase: Document analysis -> Tech detection -> Requirements
   - Planning Phase: Epic creation -> Story creation -> Technical plan
   - Implementation Phase: Code implementation -> DB engineering
   - Quality Phase: Code review -> Security scan -> Build -> Tests
   - Deployment Phase: Infrastructure provisioning -> Release

2. **Dynamic Routing**: At each checkpoint, decide the next action:
   - After code review: approve -> continue, or request changes -> loop back
   - After security scan: pass -> continue, block -> fix and re-scan
   - After tests: pass -> deploy, fail -> fix and re-test
   - After any failure: retry, escalate to supervisor, or abort

3. **Progress Tracking**: Monitor execution and report:
   - Current phase and step
   - Agent performance (timing, quality)
   - Blockers and risks
   - Iteration count for loops

4. **Conflict Resolution**: When agents produce conflicting outputs:
   - Analyze the conflict
   - Decide which approach to follow
   - Provide clear guidance for resolution

## Operational Constraints

1. **Private Repo Build Limits**: The CI Monitor agent handles the public→build→private cycle
   automatically. When reviewing CI results, be aware that:
   - The repo visibility may have been toggled during the build
   - ALL queued builds must complete before the repo goes private again
   - If CI fails and needs a re-run, the repo stays public until the fix build completes
   - Never skip the visibility restore step — escalate if it fails

2. **ArgoCD Deployments**: All K8s deployments go through ArgoCD, not kubectl.
   The Release Manager handles this, but if deployment fails, escalate rather than retry blindly.

## Decision Context

You will receive the current pipeline state including:
- The Supervisor's plan and guidance
- Results from completed agents
- A2A messages between agents
- Review feedback, security findings, test results

## Output Format

Return your routing decision as JSON:

```json
{
  "current_phase": "Name of the current phase",
  "current_step": "Name of the current step",
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
        claude = ClaudeProvider(self.config)

        # Gather execution context
        stage = state.get("stage", "triggered")
        review_decision = state.get("review_decision", "")
        security_decision = state.get("security_decision", "")
        test_failed = state.get("test_failed", 0)
        review_iteration = state.get("review_iteration", 0)
        supervisor_plan = state.get("supervisor_plan", {})
        agent_timings = state.get("agent_timings", {})

        # Build A2A context
        inbox_context = a2a.format_inbox_context()

        context_parts = [
            "## Current Pipeline State",
            f"- Stage: {stage}",
            f"- Review Decision: {review_decision or 'N/A'}",
            f"- Security Decision: {security_decision or 'N/A'}",
            f"- Test Failures: {test_failed}",
            f"- Review Iteration: {review_iteration}",
            f"- Completed Agents: {', '.join(agent_timings.keys()) or 'None'}",
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

        user_message = "\n".join(context_parts)
        user_message += "\n\nBased on the current state, what should happen next? Provide your routing decision."

        decision_text = await claude.generate(
            system_prompt=SYSTEM_PROMPT,
            user_message=user_message,
        )

        # Parse the decision
        routing = self._extract_decision(decision_text, state)

        # Calculate progress percentage based on completed stages
        total_stages = 14
        completed = len(agent_timings)
        routing["progress_pct"] = min(int((completed / total_stages) * 100), 100)

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
