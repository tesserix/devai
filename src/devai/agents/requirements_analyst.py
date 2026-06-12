"""Requirements Analyst Agent — refines raw requirements into structured, actionable specs.

Uses OpenAI for fast analysis. Identifies gaps, ambiguities,
and asks clarifying questions before passing to the Product Director.
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

SYSTEM_PROMPT = """You are a Senior Requirements Analyst at a world-class software company.

Your role is to take raw, unstructured requirements and produce:
1. A refined list of structured requirements with clear titles and descriptions
2. Identified gaps or ambiguities that need clarification
3. Assumptions made during analysis
4. Risk areas and technical considerations
5. A suggested priority ordering

For each requirement, provide:
- A clear title
- Detailed description
- Category: functional | non-functional | performance | security
- Priority: low | medium | high | critical
- Acceptance criteria (testable conditions)
- Assumptions made
- Risks identified

Output ONLY valid JSON with this structure:
{
    "requirements": [
        {
            "title": "...",
            "description": "...",
            "category": "functional",
            "priority": "high",
            "acceptance_criteria": ["..."],
            "assumptions": ["..."],
            "risks": ["..."]
        }
    ],
    "gaps": ["Questions or areas needing clarification"],
    "overall_assessment": "Brief assessment of requirement quality and readiness"
}

Be thorough but practical. Flag genuine gaps, don't invent problems."""


class RequirementsAnalystAgent(BaseAgent):
    """Analyzes and refines raw requirements using OpenAI for fast inference."""

    name = "requirements_analyst"
    subscribe_subject = "devai.pipeline.trigger"
    publish_subject = "devai.pipeline.requirements_analyzed"

    async def _execute_graph(self, state: ALMState, a2a: A2ABus) -> dict[str, Any]:
        """Analyze and refine requirements."""
        openai = OpenAIProvider(self.config)

        requirements = state.get("requirements", "")
        repo = state.get("repo_full_name", "")

        # Stack-aware refinement: this stage runs AFTER tech detection, so
        # the profile is known — requirements shaped to the actual stack
        # (its natural module boundaries, test approach, deploy story) feed
        # every downstream planner.
        from devai.agents.skills import get_skill_profile

        profile = get_skill_profile(state.get("skill_profile_name"))
        stack_guidance = profile.render_for_planner()

        prompt = f"""Repository: {repo}

{stack_guidance}

Raw Requirements:
{requirements}

Analyze these requirements thoroughly. Break them down into structured, actionable items.
Consider the repository context and the stack guidance above; identify any gaps or risks."""

        response = await openai.generate(
            prompt=prompt,
            system=SYSTEM_PROMPT,
            response_format={"type": "json_object"},
        )

        try:
            data = json.loads(response)
            analyzed = data.get("requirements", [])
            gaps = data.get("gaps", [])
            assessment = data.get("overall_assessment", "")
        except (json.JSONDecodeError, ValueError) as e:
            logger.error("Failed to parse requirements analysis: %s", e)
            analyzed = []
            gaps = [f"Analysis parsing failed: {e}"]
            assessment = "Failed to parse analysis output"

        # Notify Product Director about the analysis
        a2a.handoff(
            "product_director",
            "Requirements Analysis Complete",
            f"Analyzed {len(analyzed)} requirements. {len(gaps)} gaps identified.\n{assessment}",
            payload={"requirement_count": len(analyzed), "gap_count": len(gaps)},
        )

        # If there are significant gaps, notify Engineering Manager too
        if len(gaps) > 2:
            a2a.notify(
                "engineering_manager",
                "Requirements Gaps Detected",
                f"{len(gaps)} gaps found in requirements. Review may need extra attention:\n"
                + "\n".join(f"- {g}" for g in gaps[:5]),
            )

        return {
            "analyzed_requirements": analyzed,
            "requirement_gaps": gaps,
            "stakeholder_questions": gaps,
        }
