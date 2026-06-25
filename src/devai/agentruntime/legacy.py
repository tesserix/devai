"""``LegacyAgent`` — adapts an existing ``BaseAgent`` to the Agent SDK.

The bulk of DevAI's working code is ~20 ``BaseAgent`` subclasses under
``devai.agents`` (and the SRE agents), each with the signature
``__init__(scm, state_manager, config, event_bus)`` and an async
``run(state: ALMState) -> dict``. Today three different call sites resolve and
construct these by reflection, each with its own constructor convention:

  - ``graph/orchestrator.py``           → ``cls(scm, state, config)``
  - ``stages/specialization.py``        → ``cls(scm, state_manager, config, event_bus)``
  - ``runner/entrypoint.py``            → ``cls(None, None, config, None)``

``LegacyAgent`` is the single adapter that replaces all three. It satisfies the
``Agent`` protocol, so the dispatcher runs it exactly like any native or YAML
agent: build the ``ALMState`` from the ``RunContext`` (via the shared
``build_alm_state``), call ``agent.run(state)``, and wrap the returned patch in
a typed ``AgentResult`` (via the shared ``alm_patch_to_result``).

Construction is per-principal-correct: when the context carries a resolved SCM
client and Settings overlay (the triggering user's own credentials), the wrapped
agent is built against those — so its LLM uses the user's keys/model and its git
ops use the user's PAT / GitHub App, exactly as ``AgentAdapter`` arranges today,
but without the contextvar dance.
"""

from __future__ import annotations

import importlib
import logging
from typing import TYPE_CHECKING, Any

from devai.agentruntime.agent import AgentResult, RunContext, alm_patch_to_result, build_alm_state

if TYPE_CHECKING:
    from devai.core.base_agent import BaseAgent

logger = logging.getLogger(__name__)


class LegacyAgent:
    """Wrap a ``BaseAgent`` (instance or class) behind the ``Agent`` protocol.

    Two construction modes:

    - ``LegacyAgent(instance)`` — wrap an already-built agent. Useful when the
      caller already has one, or in tests.
    - ``LegacyAgent.from_dotted("devai.agents.foo.FooAgent")`` /
      ``LegacyAgent.from_class(FooAgent)`` — construct the agent lazily at
      ``run()`` time from the ``RunContext``, so per-principal SCM/config apply.

    Never raises out of ``run`` for construction problems: a missing optional
    dependency, an unimportable class, or absent runtime deps degrade to a
    ``stub`` ``AgentResult`` (``ok=False``), matching the graceful-degrade
    behavior of the adapters it replaces. A failure *inside* the agent's own
    ``run`` is allowed to propagate so the executor's retry/heal loop engages.
    """

    def __init__(
        self,
        agent: BaseAgent | None = None,
        *,
        agent_cls: type[BaseAgent] | None = None,
        dotted: str = "",
        name: str = "",
        output_key: str = "",
        require_deps: bool = True,
    ) -> None:
        self._agent = agent
        self._agent_cls = agent_cls
        self._dotted = dotted
        # When True (the default, used by the in-process pipeline) a missing
        # scm/state_manager degrades to a stub. The Job runner sets this False:
        # a Job pod has no Redis state_manager, and the agents tolerate None for
        # the bits they don't use — matching the old reflection path's
        # ``cls(None, None, config, None)``.
        self._require_deps = require_deps
        # Resolve a stable display name even before the class is imported.
        resolved = (
            name
            or getattr(agent, "name", "")
            or getattr(agent_cls, "name", "")
            or (dotted.rsplit(".", 1)[-1] if dotted else "")
            or (type(agent).__name__ if agent is not None else "legacy_agent")
        )
        self.name: str = resolved
        self.output_key: str = output_key or f"{resolved}_output"

    # ── Constructors ───────────────────────────────────────────────────

    @classmethod
    def from_class(
        cls,
        agent_cls: type[BaseAgent],
        *,
        name: str = "",
        output_key: str = "",
        require_deps: bool = True,
    ) -> LegacyAgent:
        return cls(agent_cls=agent_cls, name=name, output_key=output_key, require_deps=require_deps)

    @classmethod
    def from_dotted(
        cls, dotted: str, *, name: str = "", output_key: str = "", require_deps: bool = True
    ) -> LegacyAgent:
        """Defer import to ``run()`` so a missing optional dep degrades to a
        stub instead of failing at registration time."""
        return cls(dotted=dotted, name=name, output_key=output_key, require_deps=require_deps)

    # ── Agent protocol ─────────────────────────────────────────────────

    async def run(self, ctx: RunContext) -> AgentResult:
        agent = self._resolve_agent(ctx)
        if agent is None:
            logger.warning("LegacyAgent[%s]: agent unavailable — returning stub", self.name)
            return AgentResult(
                ok=False,
                stub=True,
                output_key=self.output_key,
                message=f"{self.name} skipped — agent unavailable",
                handover={f"{self.name}_stub": True},
            )

        state = build_alm_state(ctx)
        # A failure inside the agent is a real stage failure — let it propagate
        # so the blueprint executor's on_failure / retry / heal policy applies.
        patch = await agent.run(state)
        return alm_patch_to_result(patch, output_key=self.output_key, message=f"{self.name} completed")

    # ── Construction ───────────────────────────────────────────────────

    def _resolve_agent(self, ctx: RunContext) -> BaseAgent | None:
        """Return the agent instance, constructing it from the context if needed."""
        if self._agent is not None:
            return self._agent

        agent_cls = self._agent_cls or self._import_class()
        if agent_cls is None:
            return None

        # Per-principal construction: the dispatcher has already resolved the
        # triggering user's SCM client and Settings overlay onto the context.
        # Build the agent against those so it talks to the right git account and
        # LLM keys. Fall back to the shared platform deps when unresolved.
        scm = ctx.scm if ctx.scm is not None else ctx.deps.scm
        config = ctx.config if ctx.config is not None else ctx.deps.config
        state_manager = ctx.deps.state_manager
        event_bus = ctx.deps.event_bus

        if self._require_deps and (scm is None or state_manager is None):
            logger.warning(
                "LegacyAgent[%s]: missing runtime deps (scm=%s, state_manager=%s) — stub",
                self.name,
                scm is not None,
                state_manager is not None,
            )
            return None

        try:
            return agent_cls(scm, state_manager, config, event_bus)
        except Exception:  # noqa: BLE001 — construction is allowed to fail; degrade to stub
            logger.exception("LegacyAgent[%s]: construction failed", self.name)
            return None

    def _import_class(self) -> type[BaseAgent] | None:
        if not self._dotted:
            return None
        try:
            module_path, class_name = self._dotted.rsplit(".", 1)
            module = importlib.import_module(module_path)
            klass: Any = getattr(module, class_name)
            self._agent_cls = klass
            return klass
        except (ImportError, AttributeError, ValueError):
            logger.warning("LegacyAgent[%s]: cannot import %s", self.name, self._dotted, exc_info=True)
            return None


__all__ = ["LegacyAgent"]
