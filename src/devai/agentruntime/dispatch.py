"""The Agent Dispatch Kit (ADK) — one place an agent gets run.

Where ``agent.py`` defines *what an agent is*, this defines *how it runs*. The
dispatcher is the single seam that:

  1. Resolves the per-principal capabilities **once** — the triggering user's own
     LLM adapter, SCM client, and Settings overlay (the logic currently
     re-implemented in ``AgentAdapter.execute``, ``AgentRunner.run``, and
     ``_run_yaml_runner._select_llm``).
  2. Builds the ``RunContext`` from the ``DevAITask`` + ``StageDeps``.
  3. Executes the agent via a pluggable :class:`ExecutionBackend`.

``InlineBackend`` runs the agent in-process and is the default + graceful-degrade
fallback (like a Noop adapter). A future ``JobBackend`` will run the agent as a
K8s Job by reusing ``runtime/job_spec.py`` + ``job_watcher.py`` — swapping inline
for Job becomes a backend choice, not a separate code path. Because the dispatcher
attaches itself to every ``RunContext``, an agent can recurse into sub-agents via
``ctx.spawn(...)`` — the ROMA / RecursiveMAS decomposition pattern, on one seam.

The dispatcher never reflects on a concrete agent type; it only calls
``agent.run(ctx)``. That is what makes ``LegacyAgent``, ``SpecAgent``, and any
native agent interchangeable across inline / Job / recursive execution.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from devai.agentruntime.agent import Agent, AgentResult, RunContext

if TYPE_CHECKING:
    from devai.adapters.llm.base import LLMAdapter
    from devai.config import Settings
    from devai.pipeline.interfaces import StageDeps
    from devai.pipeline.types import DevAITask
    from devai.scm.base import SCMClient

logger = logging.getLogger(__name__)


def _is_human(email: str) -> bool:
    """A real user (not a webhook/system/cron synthetic principal)."""
    return bool(email) and "@" in email and not email.startswith(("webhook:", "system:"))


class ExecutionBackend(ABC):
    """How a single ``agent.run(ctx)`` is physically carried out.

    The contract is intentionally tiny: given an agent and its context, produce
    an ``AgentResult``. Concrete backends decide *where* that happens (this pod,
    a K8s Job, a warm worker pool). Backends MUST NOT swallow exceptions raised
    by the agent itself — a real failure must reach the executor so its
    retry/heal policy engages.
    """

    @property
    def name(self) -> str:
        return type(self).__name__

    @abstractmethod
    async def run(self, agent: Agent, ctx: RunContext) -> AgentResult: ...


class InlineBackend(ExecutionBackend):
    """Run the agent in the current process.

    The default and the graceful-degrade fallback: it has no external
    dependencies and always works, so a deployment with the Job runtime disabled
    (``k8s_runtime_enabled=False``, the current prod default) still runs every
    agent through the same dispatcher.
    """

    async def run(self, agent: Agent, ctx: RunContext) -> AgentResult:
        return await agent.run(ctx)


class AgentDispatcher:
    """Resolve, contextualize, and run agents through a pluggable backend.

    Construct one per stage/run with the shared ``StageDeps`` and (optionally) a
    backend; ``InlineBackend`` is used when none is given. The same dispatcher
    instance is attached to every ``RunContext`` it builds, enabling recursion.
    """

    def __init__(self, deps: StageDeps, backend: ExecutionBackend | None = None) -> None:
        self.deps = deps
        self.backend: ExecutionBackend = backend or InlineBackend()

    # ── Public API ─────────────────────────────────────────────────────

    async def dispatch(
        self,
        agent: Agent,
        task: DevAITask,
        *,
        instruction: str = "",
        extra_context: dict | None = None,
        config: Settings | None = None,
        scm: SCMClient | None = None,
    ) -> AgentResult:
        """Run one agent for ``task`` and return its typed result.

        ``config``/``scm`` let a caller that has already resolved the principal's
        overlay + SCM client (e.g. ``AgentStage`` via ``resolve_principal_run``)
        pass them straight through instead of having the dispatcher re-resolve.
        """
        ctx = await self.build_context(
            task,
            instruction=instruction,
            extra_context=extra_context,
            config_override=config,
            scm_override=scm,
        )
        return await self.backend.run(agent, ctx)

    async def dispatch_many(
        self,
        agents: list[Agent],
        task: DevAITask,
    ) -> list[AgentResult]:
        """Run several agents on the same task concurrently (the mixture pattern).

        Shares one resolved ``RunContext`` across the agents — they read the same
        handover bag and identity but produce independent results. A backend or
        agent error surfaces as an exception in the corresponding slot; callers
        that want soft failures should wrap their agents. Order matches ``agents``.
        """
        if not agents:
            return []
        ctx = await self.build_context(task)
        results = await asyncio.gather(
            *(self.backend.run(agent, ctx) for agent in agents),
            return_exceptions=True,
        )
        out: list[AgentResult] = []
        for agent, res in zip(agents, results, strict=True):
            if isinstance(res, BaseException):
                logger.exception("AgentDispatcher.dispatch_many: %s failed", getattr(agent, "name", "?"))
                out.append(
                    AgentResult(ok=False, error=str(res), message=f"{getattr(agent, 'name', '?')} failed")
                )
            else:
                out.append(res)
        return out

    # ── Context construction (the single per-principal resolution point) ─

    async def build_context(
        self,
        task: DevAITask,
        *,
        instruction: str = "",
        extra_context: dict | None = None,
        config_override: Settings | None = None,
        scm_override: SCMClient | None = None,
    ) -> RunContext:
        """Resolve the triggering user's capabilities and assemble a RunContext.

        This is the one place per-principal resolution happens — every agent,
        every backend, sees the same wiring:

          - ``llm``    — ``deps.llm_for_principal(email)`` (their connector → trial
            → platform default), never raising.
          - ``scm``    — ``deps.scm_for_principal(email)`` (their PAT / GitHub App
            → platform client), unless ``scm_override`` is supplied.
          - ``config`` — the principal's Settings overlay (their keys/model) when
            they have their own LLM connector; else ``deps.config``. ``config_override``
            wins when supplied (the caller already resolved it, e.g. with the trial gate).
        """
        email = task.triggered_by or ""
        deps = self.deps

        llm = await self._resolve_llm(email)
        scm = scm_override if scm_override is not None else await self._resolve_scm(email)
        config = config_override if config_override is not None else await self._resolve_config(email)

        return RunContext(
            task=task,
            deps=deps,
            llm=llm,
            scm=scm,
            config=config,
            instruction=instruction,
            extra_context=extra_context or {},
            dispatcher=self,
        )

    async def _resolve_llm(self, email: str) -> LLMAdapter | None:
        try:
            return await self.deps.llm_for_principal(email)
        except Exception:  # noqa: BLE001 — resolution must never break a run
            logger.debug("AgentDispatcher: llm_for_principal failed — using deps.llm", exc_info=True)
            return self.deps.llm

    async def _resolve_scm(self, email: str) -> SCMClient | None:
        try:
            return await self.deps.scm_for_principal(email) or self.deps.scm
        except Exception:  # noqa: BLE001
            logger.debug("AgentDispatcher: scm_for_principal failed — using deps.scm", exc_info=True)
            return self.deps.scm

    async def _resolve_config(self, email: str) -> Settings:
        """The principal's Settings overlay (their keys/model), or the platform
        config. Only a human with their own LLM connector gets an overlay; system
        principals and connector-less users ride the platform config."""
        resolver = getattr(self.deps, "llm_resolver", None)
        if resolver is None or not _is_human(email) or not hasattr(resolver, "llm_overlay_for_email"):
            return self.deps.config
        try:
            overlay, has_own = await resolver.llm_overlay_for_email(email)
            if has_own and overlay is not None:
                return overlay
        except Exception:  # noqa: BLE001
            logger.debug("AgentDispatcher: config overlay resolution failed", exc_info=True)
        return self.deps.config


__all__ = ["AgentDispatcher", "ExecutionBackend", "InlineBackend"]
