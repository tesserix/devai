"""`run_crew` stage — runs an AI agent crew on a task.

A crew = a lead + members, each member a specialization with its own skills.
This stage orchestrates them:

  1. **Resolve** the crew — from the DB (`task.crew_id` via TeamService) or a
     seed crew (`crews/*.yaml`), selected by `config.crew` / task context.
  2. **Plan** — the lead runs first (an `AgentRunner` call) and proposes
     `assignments`: which member does what. If the lead doesn't return a
     structured plan, every non-lead member is assigned the original intent.
  3. **Execute** — each assignment runs the target member via `AgentRunner`,
     with its crew-specific `allowed_tools` and the accumulated outputs of
     earlier members as context. Each step is recorded as an A2A-style
     handoff→response on the crew trail.
  4. **Hand over** — member outputs are merged into the task's agent_context
     under `crew_output` so downstream stages (review/security/PR) see them.

Each member is one `AgentRunner` invocation, so the same loop, tool-gating,
and handover validation built in Phase 0 apply per member. When the stage
runs inside the runner pod (`deps.extra["workdir"]` set), the members'
shell/checkpoint tools operate on the shared git worktree; in-process they
coordinate purely through the LLM + SCM.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import replace
from typing import Any

from devai.pipeline.interfaces import PipelineStage, StageDeps
from devai.pipeline.types import DevAITask, StageResult, TaskState

logger = logging.getLogger(__name__)


class _CrewRunnerStage(PipelineStage):
    def __init__(self, deps: StageDeps, config: dict[str, str]) -> None:
        self.deps = deps
        self.config = config
        # Optional seed-crew name override from the blueprint; otherwise the
        # crew is chosen from the task (crew_id → DB, or agent_context["crew"]).
        self.crew_name = config.get("crew") or config.get("crew_spec") or ""

    def name(self) -> str:
        return f"run_crew:{self.crew_name or 'dynamic'}"

    async def execute(self, task: DevAITask) -> StageResult:
        crew = await self._resolve_crew(task)
        if crew is None:
            return StageResult(
                message="no crew resolved (set task.crew_id, config.crew, or agent_context['crew'])",
                data={"crew_error": "no_crew"},
            )

        from devai.agentruntime.runner import AgentRunner

        runner = AgentRunner(self.deps)
        trail: list[dict[str, Any]] = []
        member_outputs: dict[str, Any] = {}

        # ── 1. Lead plans the work ────────────────────────────────────
        lead_spec = self._resolve_member_spec(crew, crew.lead)
        assignments = await self._plan(runner, crew, lead_spec, task, trail)

        # ── 2. Members execute their assignments ──────────────────────
        for assignment in assignments:
            member_name = assignment["member"]
            subtask = assignment["task"]
            member_spec = self._resolve_member_spec(crew, member_name)
            if member_spec is None:
                logger.warning("crew %s: member %s not in spec catalog — skipping", crew.name, member_name)
                continue
            trail.append(_msg(crew.lead, member_name, "handoff", subtask))
            run = await runner.run(
                member_spec,
                task,
                instruction=subtask,
                extra_context={"crew_member_outputs": member_outputs} if member_outputs else None,
            )
            member_outputs[member_name] = run.patch
            summary = run.error or _summarize(run.patch)
            trail.append(_msg(member_name, crew.lead, "response", summary))

        await self._publish_trail(task, trail)

        crew_output = {
            "crew": crew.name,
            "lead": crew.lead,
            "members": [m.specialization for m in crew.members],
            "assignments": assignments,
            "member_outputs": member_outputs,
            "trail": trail,
        }
        task.agent_context.setdefault("crew_trail", []).extend(trail)
        return StageResult(
            next_state=TaskState.RUNNING,
            message=f"crew {crew.name} ran {len(assignments)} assignment(s) across {len(crew.members)} member(s)",
            data={"crew_output": crew_output, **member_outputs},
        )

    # ── crew resolution ───────────────────────────────────────────────

    async def _resolve_crew(self, task: DevAITask):
        from devai.crews.models import CrewSpec

        extra = self.deps.extra or {}

        # 1. DB-backed crew assigned to the task
        team_service = extra.get("team_service")
        if task.crew_id and team_service is not None:
            try:
                row = await team_service.get_crew(task.crew_id)
                if row:
                    return CrewSpec.from_dict(row)
            except Exception:  # noqa: BLE001
                logger.warning("crew %s lookup failed — falling back to seeds", task.crew_id, exc_info=True)

        # 2. seed crew by name (blueprint config, task context, or DB-injected map)
        wanted = self.crew_name or (task.agent_context or {}).get("crew") or ""
        seeds = extra.get("seed_crews")
        if seeds is None:
            from devai.crews.loader import load_seed_crews

            crews_dir = getattr(self.deps.config, "crews_dir", "crews")
            seeds = load_seed_crews(crews_dir)
        if wanted and wanted in seeds:
            return seeds[wanted]
        # last resort: if exactly one seed exists, use it
        if len(seeds) == 1:
            return next(iter(seeds.values()))
        return None

    def _resolve_member_spec(self, crew, specialization: str):
        spec = self._spec(specialization)
        if spec is None:
            return None
        member = crew.member_for(specialization)
        if member and member.allowed_tools:
            # crew-specific tool grant overrides the spec's default
            return replace(spec, allowed_tools=list(member.allowed_tools))
        return spec

    def _spec(self, name: str):
        extra = self.deps.extra or {}
        registry = extra.get("specialization_registry")
        if registry is None:
            from devai.specializations.registry import SpecializationRegistry

            spec_dir = getattr(self.deps.config, "specializations_dir", "specializations")
            try:
                registry = SpecializationRegistry.from_directory(spec_dir)
                # cache on extra so repeated lookups are cheap
                if isinstance(self.deps.extra, dict):
                    self.deps.extra["specialization_registry"] = registry
            except Exception:  # noqa: BLE001
                logger.exception("crew: could not load specializations from %s", spec_dir)
                return None
        try:
            return registry.resolve(name)
        except Exception:  # noqa: BLE001
            return None

    # ── planning ──────────────────────────────────────────────────────

    async def _plan(
        self, runner, crew, lead_spec, task: DevAITask, trail: list[dict[str, Any]]
    ) -> list[dict[str, str]]:
        """Ask the lead to propose assignments; fall back to one-per-member."""
        fallback = [{"member": m.specialization, "task": task.intent} for m in (crew.non_lead_members or crew.members)]
        if lead_spec is None or self.deps.llm is None:
            return fallback

        member_list = ", ".join(
            f"{m.specialization} ({m.role_label})" if m.role_label else m.specialization for m in crew.members
        )
        instruction = (
            f"You are the lead of a crew working on this task:\n\n{task.intent}\n\n"
            f"Your crew members are: {member_list}.\n"
            "Break the work into assignments and reply with ONLY a JSON object of the form "
            '{"assignments": [{"member": "<specialization>", "task": "<what they should do>"}]}. '
            "Use only the member specialization names listed above. Keep it to the few assignments that matter."
        )
        try:
            run = await runner.run(lead_spec, task, instruction=instruction)
        except Exception:  # noqa: BLE001
            logger.warning("crew %s: lead planning failed — using fallback", crew.name, exc_info=True)
            return fallback

        raw = run.patch.get("assignments") if isinstance(run.patch, dict) else None
        valid_members = {m.specialization for m in crew.members}
        assignments: list[dict[str, str]] = []
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                member = str(item.get("member") or "")
                subtask = str(item.get("task") or "").strip()
                if member in valid_members and subtask:
                    assignments.append({"member": member, "task": subtask})
        if assignments:
            trail.append(_msg(crew.lead, "crew", "plan", f"{len(assignments)} assignment(s)"))
            return assignments
        return fallback

    # ── A2A trail ─────────────────────────────────────────────────────

    async def _publish_trail(self, task: DevAITask, trail: list[dict[str, Any]]) -> None:
        """Best-effort: push the crew's A2A messages to the live event bus.

        Persisted on the task regardless (agent_context["crew_trail"]); this
        only adds the live dashboard feed. Lazy + swallowed so a missing bus
        never fails the crew.
        """
        bus = self.deps.event_bus_adapter
        if bus is None:
            return
        try:
            for msg in trail:
                await bus.publish(f"devai.crew.{task.id}.message", msg)
        except Exception:  # noqa: BLE001
            logger.debug("crew trail publish failed", exc_info=True)


def _msg(from_agent: str, to_agent: str, kind: str, body: str) -> dict[str, Any]:
    return {
        "id": uuid.uuid4().hex[:12],
        "from_agent": from_agent,
        "to_agent": to_agent,
        "message_type": kind,
        "body": body[:1000],
    }


def _summarize(patch: Any) -> str:
    if isinstance(patch, dict):
        if patch.get("summary"):
            return str(patch["summary"])[:500]
        return ", ".join(sorted(patch.keys()))[:500]
    return str(patch)[:500]


def crew_runner_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    """Stage factory registered with the StageRegistry under `run_crew`."""
    return _CrewRunnerStage(deps, config)


__all__ = ["crew_runner_stage"]
