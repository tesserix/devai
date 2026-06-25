"""`collaborate` stage — run a multi-agent collaboration pattern over specs.

Exposes the Agent SDK's collaboration patterns (sequential / mixture /
deliberation / distillation) to blueprint authors declaratively: name the
pattern + the specializations it composes, and the stage dispatches them through
the one ``AgentDispatcher``. This is the RecursiveMAS taxonomy made usable from
YAML — no Python, no redeploy.

    # fan out specialists concurrently and aggregate (mixture):
    - stage: collaborate
      config:
        pattern: mixture
        agents: requirements_analyst,tech_detector
        output_key: analysis

    # actor ↔ critic loop until the critic approves (deliberation):
    - stage: collaborate
      config:
        pattern: deliberation
        actor: prototyper
        critic: reflector
        accept_field: approved   # the critic's handover key that gates the loop
        max_rounds: "3"

    # cheap learner first, escalate to the expert on uncertainty (distillation):
    - stage: collaborate
      config:
        pattern: distillation
        learner: requirements_analyst
        expert: engineering_manager
"""

from __future__ import annotations

import logging
from typing import Any

from devai.pipeline.interfaces import PipelineStage, StageDeps
from devai.pipeline.types import DevAITask, StageResult

logger = logging.getLogger(__name__)

_PATTERNS = ("sequential", "mixture", "deliberation", "distillation")


class _CollaborateStage(PipelineStage):
    def __init__(self, deps: StageDeps, config: dict[str, str]) -> None:
        self.deps = deps
        self.config = config
        self.pattern = (config.get("pattern") or "sequential").strip().lower()
        self.output_key = config.get("output_key") or f"collaborate_{self.pattern}_output"

    def name(self) -> str:
        return f"collaborate:{self.pattern}"

    async def execute(self, task: DevAITask) -> StageResult:
        from devai.agentruntime import AgentDispatcher, AgentResult, deliberation, distillation, mixture, sequential

        if self.pattern not in _PATTERNS:
            return self._skip(f"unknown collaboration pattern {self.pattern!r} (use one of {', '.join(_PATTERNS)})")

        dispatcher = AgentDispatcher(self.deps)

        if self.pattern == "deliberation":
            actor = self._agent(self.config.get("actor"))
            critic = self._agent(self.config.get("critic"))
            if actor is None or critic is None:
                return self._skip("deliberation needs config.actor + config.critic (both resolvable specs)")
            field = (self.config.get("accept_field") or "approved").strip()
            result = await deliberation(
                dispatcher,
                actor,
                critic,
                task,
                max_rounds=_int(self.config.get("max_rounds"), 3),
                accept=lambda r: bool(r.handover.get(field)),
            )
        elif self.pattern == "distillation":
            learner = self._agent(self.config.get("learner"))
            expert = self._agent(self.config.get("expert"))
            if learner is None or expert is None:
                return self._skip("distillation needs config.learner + config.expert (both resolvable specs)")
            result = await distillation(dispatcher, learner, expert, task)
        else:  # mixture | sequential
            agents = self._agents(self.config.get("agents"))
            if not agents:
                return self._skip(f"{self.pattern} needs config.agents (comma-separated spec names)")
            if self.pattern == "mixture":
                result = await mixture(dispatcher, agents, task)
            else:
                results = await sequential(dispatcher, agents, task)
                result = results[-1] if results else AgentResult(ok=False, message="sequential ran no agents")

        return StageResult(
            next_state=result.next_state,
            message=f"collaborate:{self.pattern} produced {sorted(result.handover)[:6]}",
            data={self.output_key: result.handover},
        )

    # ── spec → agent resolution ───────────────────────────────────────

    def _agent(self, name: str | None) -> Any:
        spec = self._spec((name or "").strip())
        if spec is None:
            return None
        from devai.agentruntime import SpecAgent

        return SpecAgent(spec)

    def _agents(self, raw: str | None) -> list[Any]:
        names = [n.strip() for n in (raw or "").split(",") if n.strip()]
        return [a for a in (self._agent(n) for n in names) if a is not None]

    def _spec(self, name: str) -> Any:
        if not name:
            return None
        extra = self.deps.extra or {}
        registry = extra.get("specialization_registry")
        if registry is None:
            from devai.specializations.registry import SpecializationRegistry

            spec_dir = getattr(self.deps.config, "specializations_dir", "specializations")
            try:
                registry = SpecializationRegistry.from_directory(spec_dir)
            except Exception:  # noqa: BLE001
                logger.exception("collaborate: could not load specializations from %s", spec_dir)
                return None
        try:
            return registry.resolve(name)
        except Exception:  # noqa: BLE001
            logger.warning("collaborate: spec %r not in catalog — skipping", name)
            return None

    def _skip(self, reason: str) -> StageResult:
        logger.warning("collaborate:%s skipped — %s", self.pattern, reason)
        return StageResult(message=f"collaborate:{self.pattern} skipped — {reason}", data={f"{self.output_key}_error": reason})


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def collaborate_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    """Stage factory registered with the StageRegistry under `collaborate`."""
    return _CollaborateStage(deps, config)


__all__ = ["collaborate_stage"]
