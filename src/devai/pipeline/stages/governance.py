"""Governance maintenance — keep the target repo's CLAUDE.md alive.

CLAUDE.md is the governance file every agent reads (context hydration
injects it into their prompts as hard rules). The scaffold seeds a generic
one; this stage UPDATES it with what the run actually learned — detected
stack, project structure, commands, conventions, and the approved plan /
boardroom decision — committed to the run's working branch so it ships
with the PR and steers every future run and reviewer.

Contract:
  - The existing "Critical Rules" content is PRESERVED VERBATIM — the
    stage enriches, it never weakens guardrails.
  - No usable LLM → a mechanical compose from the run's structured fields
    (still better than a stale generic file).
  - Failures never block delivery (wired on_failure: continue after the
    recovery agent's attempts).
"""

from __future__ import annotations

import logging

from devai.pipeline.interfaces import PipelineStage, StageDeps
from devai.pipeline.types import DevAITask, StageResult

logger = logging.getLogger(__name__)


class _UpdateGovernanceStage(PipelineStage):
    def __init__(self, deps: StageDeps, config: dict[str, str]) -> None:
        self.deps = deps
        self.config = config

    def name(self) -> str:
        return str(self.config.get("__stage_name") or "update-governance")

    async def execute(self, task: DevAITask) -> StageResult:
        scm = self.deps.scm
        if scm is None or not task.repo or getattr(task, "dry_run", False):
            return StageResult(
                message="governance update skipped (no SCM / dry run)",
                data={"governance_updated": False},
            )

        branch = await self._working_branch(task)
        existing = ""
        try:
            existing = await scm.get_file_content(task.repo, "CLAUDE.md", branch)
        except Exception:  # noqa: BLE001 — no file yet is fine, we create it
            logger.info("update_governance: no existing CLAUDE.md on %s@%s", task.repo, branch)

        structure = await self._project_structure(task, branch)
        content = await self._compose(task, existing, structure)
        if not content.strip():
            return StageResult(
                message="governance compose produced nothing — left CLAUDE.md untouched",
                data={"governance_updated": False},
            )

        try:
            await scm.create_or_update_file(
                task.repo,
                "CLAUDE.md",
                content,
                f"docs: refresh CLAUDE.md with the stack, structure, and approved plan (run {task.id})",
                branch,
            )
        except Exception:  # noqa: BLE001
            logger.exception("update_governance: commit failed")
            raise

        return StageResult(
            message=f"CLAUDE.md refreshed on {branch}",
            data={"governance_updated": True, "governance_branch": branch},
        )

    async def _working_branch(self, task: DevAITask) -> str:
        redis = getattr(self.deps.state_manager, "redis", None)
        if redis is not None:
            try:
                wb = await redis.get(f"devai:run:{task.id}:workbranch")
                if wb:
                    return str(wb)
            except Exception:  # noqa: BLE001
                pass
        return task.branch_name or "main"

    async def _project_structure(self, task: DevAITask, branch: str) -> str:
        """Top-level layout + the package commands — concrete repo facts."""
        lines: list[str] = []
        try:
            entries = await self.deps.scm.list_files(task.repo, "", branch)
            if isinstance(entries, list):
                for e in entries[:25]:
                    kind = "dir" if str(e.get("type", "")) in ("dir", "tree") else "file"
                    lines.append(f"- {e.get('name') or e.get('path')} ({kind})")
        except Exception:  # noqa: BLE001
            pass
        try:
            import json as _json

            pkg = await self.deps.scm.get_file_content(task.repo, "package.json", branch)
            scripts = (_json.loads(pkg) or {}).get("scripts") or {}
            if scripts:
                lines.append("\nCommands (package.json scripts):")
                for k, v in list(scripts.items())[:12]:
                    lines.append(f"- `npm run {k}` — {str(v)[:80]}")
        except Exception:  # noqa: BLE001
            pass
        return "\n".join(lines)

    def _skill_guidance(self, ctx: dict) -> str:
        """The active skill profile's rendered conventions — directory layout,
        test framework, file idioms — so the guide matches how this stack is
        actually worked, not generic advice."""
        try:
            from devai.agents.skills import get_skill_profile

            profile = get_skill_profile(ctx.get("skill_profile_name"))
            parts = [profile.render_for_developer()]
            qa = getattr(profile, "render_for_qa", None)
            if callable(qa):
                parts.append(qa())
            return "\n\n".join(p for p in parts if p)[:3000]
        except Exception:  # noqa: BLE001
            return ""

    async def _compose(self, task: DevAITask, existing: str, structure: str) -> str:
        ctx = task.agent_context
        facts = {
            "repo": task.repo,
            "intent": (task.intent or "")[:600],
            "tech_stack": str(ctx.get("detected_tech_stack") or "")[:400],
            "skill_profile": str(ctx.get("skill_profile_name") or ""),
            "plan": str(ctx.get("boardroom_decision") or ctx.get("technical_plan") or "")[:2000],
            "epic": task.epic_issue_number,
            "pr": task.pr_number,
        }
        skill_guidance = self._skill_guidance(ctx)
        llm = self.deps.llm
        if llm is not None and getattr(llm, "provider_name", "noop") != "noop":
            try:
                from devai.adapters.llm.base import LLMMessage, LLMRequest, LLMRole

                prompt = (
                    "Update this repository's CLAUDE.md guide. It is read by Claude "
                    "(and every engineering agent) at the start of ANY future work on this "
                    "repo, so it must read like the repo's own engineering handbook — match "
                    "the project's logic, stack, and conventions exactly.\n\n"
                    f"CURRENT CLAUDE.md:\n---\n{existing[:4000] or '(none yet)'}\n---\n\n"
                    f"WHAT THIS DELIVERY RUN LEARNED:\n"
                    f"- Repo: {facts['repo']}\n- Requirement: {facts['intent']}\n"
                    f"- Detected tech stack: {facts['tech_stack'] or 'unknown'}\n"
                    f"- Skill profile: {facts['skill_profile'] or 'unknown'}\n"
                    f"- Approved plan/decision:\n{facts['plan'] or '(none recorded)'}\n\n"
                    f"STACK SKILLS & CONVENTIONS (from the active skill profile — fold these in):\n"
                    f"{skill_guidance or '(none rendered)'}\n\n"
                    f"PROJECT STRUCTURE (ground truth):\n{structure or '(unavailable)'}\n\n"
                    "RULES:\n"
                    "1. PRESERVE every existing 'Critical Rules' section VERBATIM — never weaken guardrails.\n"
                    "2. Add or refresh these sections with the facts above: '## Project Overview', "
                    "'## Tech Stack', '## Project Structure', '## Commands', "
                    "'## Skills & Conventions' (directory layout, naming, component/test idioms "
                    "from the skill profile — concrete, repo-specific), "
                    "'## Testing' (framework, where tests live, how to run them), "
                    "'## Architecture Decisions' (from the approved plan, with the why).\n"
                    "3. Be concrete and concise — this file is injected into every agent's prompt.\n"
                    "4. Do NOT mention AI tools, assistants, or how this file is generated.\n"
                    "Reply with the COMPLETE new CLAUDE.md content only — no fences, no commentary."
                )
                response = await llm.generate(
                    LLMRequest(
                        messages=[LLMMessage(role=LLMRole.USER, content=prompt)],
                        max_tokens=2500,
                        temperature=0.2,
                        extra={"agent": "governance"},
                    )
                )
                text = (response.text or "").strip()
                if text.startswith("```"):
                    text = text.split("\n", 1)[1].rsplit("```", 1)[0]
                # Guardrail check: refuse an update that DROPPED the rules.
                if existing and "Critical Rules" in existing and "Critical Rules" not in text:
                    logger.warning("update_governance: LLM dropped Critical Rules — using mechanical merge")
                else:
                    return text
            except Exception:  # noqa: BLE001
                logger.exception("update_governance: LLM compose failed — mechanical merge")

        # Mechanical merge: keep the existing file, append/refresh a facts block.
        marker = "<!-- devai:facts -->"
        block = (
            f"{marker}\n## Project Overview\n{facts['intent']}\n\n"
            f"## Tech Stack\n{facts['tech_stack'] or 'See repository configuration.'}\n\n"
            f"## Project Structure\n{structure or '(see repository root)'}\n\n"
            + (f"## Skills & Conventions\n{skill_guidance}\n\n" if skill_guidance else "")
            + (f"## Architecture Decisions\n{facts['plan']}\n" if facts["plan"] else "")
        )
        base = existing.split(marker)[0].rstrip() if existing else f"# Claude Reference Guide — {task.repo}"
        return f"{base}\n\n{block}\n"


def update_governance_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    return _UpdateGovernanceStage(deps, config)


__all__ = ["update_governance_stage"]
