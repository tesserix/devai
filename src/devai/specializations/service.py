"""SpecializationService — FastAPI lifecycle wrapper for the registry.

Mirrors PipelineService. Holds one SpecializationRegistry, loaded from
`settings.specializations_dir`. Stored on `app.state.specialization_service`.

Other parts of DevAI (PipelineService, dashboard, chat agent) read from
this service rather than building their own registry — that way a SIGHUP
reload propagates.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from devai.specializations.registry import (
    SpecializationRegistry,
    SpecializationRegistryError,
)

if TYPE_CHECKING:
    from devai.config import Settings
    from devai.specializations.base import Specialization

logger = logging.getLogger(__name__)


class SpecializationService:
    def __init__(
        self,
        config: Settings,
        *,
        directory: str | Path | None = None,
        registry_client: Any | None = None,
    ) -> None:
        self.config = config
        self.directory = Path(directory or getattr(config, "specializations_dir", "specializations"))
        # When the aregistry client is available, the service prefers
        # it as the source of truth and uses local YAML only as a
        # fallback. Pass-through None reverts to the original
        # local-only mode (the unit tests rely on that).
        self._registry_client = registry_client
        self._registry: SpecializationRegistry | None = None
        self._started = False
        self._source = "local"  # exposed via list_all() for the dashboard

    async def start(self) -> None:
        if self._started:
            return
        registry = await self._load()
        self._registry = registry
        self._started = True

    async def _load(self) -> SpecializationRegistry:
        """Load from aregistry if a client is configured + reachable;
        otherwise fall back to local YAML. The merger is union: anything
        the registry doesn't have is filled in from the local seeds, so
        partial registry coverage doesn't break the runtime."""
        local_registry = SpecializationRegistry()
        try:
            local_registry = SpecializationRegistry.from_directory(self.directory)
        except Exception:
            logger.exception("specializations: local YAML load failed — starting with empty")

        if self._registry_client is None:
            self._source = "local"
            logger.info(
                "specializations: loaded %d specs (source=local YAML at %s)",
                len(local_registry),
                self.directory,
            )
            return local_registry

        try:
            # Layer the registry over the local YAML. Local seeds carry runtime
            # metadata (tools, handover schema, context keys) that aregistry's
            # slimmer schema drops, so disk wins wherever both define a role;
            # the registry contributes the roles disk has never heard of, which
            # is what makes an agent authored in the UI actually runnable.
            cat_skills = self._registry_client.list_skills()
            cat_agents = self._registry_client.list_agents()
            adopted = self._adopt_published_agents(local_registry, cat_agents)
            self._source = "registry+local" if (cat_skills or cat_agents) else "local"
            logger.info(
                "specializations: loaded %d local specs; aregistry advertises %d skills + %d agents, "
                "%d adopted as runnable (source=%s)",
                len(local_registry),
                len(cat_skills),
                len(cat_agents),
                adopted,
                self._source,
            )
        except Exception:  # noqa: BLE001
            logger.exception("specializations: aregistry consultation failed — falling back to local")
            self._source = "local"
        return local_registry

    def _adopt_published_agents(self, registry: SpecializationRegistry, agents: list[Any]) -> int:
        """Register published agents that disk does not already define.

        A malformed published agent is skipped, never fatal: one bad artifact in
        a shared catalog must not take the whole runtime down.
        """
        from devai.registry.mapping import agent_envelope_to_spec

        adopted = 0
        for agent in agents:
            raw = getattr(agent, "raw", None)
            if not isinstance(raw, dict) or not raw:
                continue
            try:
                spec = agent_envelope_to_spec(raw)
            except Exception as exc:  # noqa: BLE001 — one bad artifact, not a bad catalog
                logger.warning("specializations: skipping published agent %r — %s", getattr(agent, "name", "?"), exc)
                continue
            if registry.has(spec.name):
                continue
            registry.register(spec)
            adopted += 1
        return adopted

    @property
    def source(self) -> str:
        """`local` | `registry+local`. Surfaced through the dashboard's
        registry panel so operators can see whether the catalog is
        live."""
        return self._source

    async def stop(self) -> None:
        self._started = False
        self._registry = None

    async def reload(self) -> int:
        """Re-discover, from disk *and* the registry. Returns the new count.

        Used by the dashboard's "reload specs" action (and a future
        SIGHUP handler) so YAML edits take effect without a restart. It goes
        through the same load as start(): a disk-only rebuild would silently
        drop every agent published from the UI.
        """
        new_registry = await self._load()
        self._registry = new_registry
        return len(new_registry)

    async def resolve_runnable(self, name: str) -> Specialization | None:
        """The role, re-consulting the catalog once if it is not known yet.

        An agent published from the UI is invokable straight away instead of
        after the next restart — that is the whole author→run loop.
        """
        spec = self.get_full(name)
        if spec is not None or self._registry_client is None:
            return spec
        await self.reload()
        return self.get_full(name)

    # ── Read surface ─────────────────────────────────────────────────

    @property
    def registry(self) -> SpecializationRegistry:
        if self._registry is None:
            return SpecializationRegistry()
        return self._registry

    def list_all(self) -> list[dict[str, Any]]:
        return [s.to_dict() for s in self.registry.all()]

    def by_category(self) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {}
        for spec in self.registry.all():
            out.setdefault(spec.category, []).append(spec.to_dict())
        return out

    def get(self, name: str) -> dict[str, Any] | None:
        try:
            return self.registry.resolve(name).to_dict()
        except SpecializationRegistryError:
            return None

    def get_full(self, name: str) -> Specialization | None:
        try:
            return self.registry.resolve(name)
        except SpecializationRegistryError:
            return None

    # ── Execution surface ────────────────────────────────────────────

    async def invoke(
        self,
        agent_name: str,
        state: dict[str, Any],
        *,
        deps: Any = None,
    ) -> dict[str, Any] | None:
        """Run a YAML specialization through the Agent SDK and return its patch.

        This is the method the Job runner's entrypoint calls. It handles
        **YAML-only** specs (no ``legacy_python_class``) by dispatching a
        ``SpecAgent`` — the same unified runner the in-process pipeline uses, so
        a YAML role behaves identically as a Job or inline. (Before this, a
        YAML-only role could not run as a Job at all — the entrypoint's only
        fallback was reflection-loading a ``devai.agents.<name>`` Python class.)

        Returns ``None`` when the agent is unknown or is a legacy Python class,
        so the caller falls back to constructing that class directly — ``invoke``
        itself never reflection-loads Python by name.

        ``deps`` is for tests; in production a minimal ``StageDeps`` (LLM + SCM
        built from ``config``, no Redis) is constructed.
        """
        spec = self.get_full(agent_name)
        if spec is None or spec.uses_legacy_runtime:
            return None

        run_deps = deps if deps is not None else self._build_runner_deps()
        task = self._task_from_state(state)

        from devai.agentruntime import AgentDispatcher, SpecAgent

        result = await AgentDispatcher(run_deps).dispatch(SpecAgent(spec), task)
        patch: dict[str, Any] = (
            dict(result.handover) if isinstance(result.handover, dict) else {"value": result.handover}
        )
        patch.setdefault("ok", not result.error)
        patch.setdefault("final_text", result.final_text)
        patch.setdefault(
            "usage",
            {
                "prompt_tokens": result.prompt_tokens,
                "completion_tokens": result.completion_tokens,
                "total_tokens": result.prompt_tokens + result.completion_tokens,
                "tool_calls": result.tool_calls,
                "turns": result.turns,
            },
        )
        if result.trace_steps:
            patch.setdefault("trace_steps", result.trace_steps)
        if result.final_text and f"{spec.name}_text" not in patch:
            patch[f"{spec.name}_text"] = result.final_text
        return patch

    def _build_runner_deps(self) -> Any:
        """A minimal StageDeps for a Job pod: LLM + SCM from config, no Redis.

        Never raises — a backend that fails to build degrades to ``None`` and
        the spec runs with whatever is available (an absent LLM yields a stub)."""
        from devai.pipeline.interfaces import StageDeps

        llm = None
        try:
            from devai.adapters.llm.factory import create_llm_adapter

            llm = create_llm_adapter(self.config)
        except Exception:  # noqa: BLE001
            logger.debug("invoke: LLM adapter construction failed", exc_info=True)
        scm = None
        try:
            from devai.scm import create_scm_client

            scm = create_scm_client(self.config)
        except Exception:  # noqa: BLE001
            logger.debug("invoke: SCM client construction failed", exc_info=True)
        return StageDeps(config=self.config, llm=llm, scm=scm)

    @staticmethod
    def _task_from_state(state: dict[str, Any]) -> Any:
        """Project the runner's ALMState-shaped slice onto a DevAITask."""
        from devai.pipeline.types import DevAITask

        principal = state.get("principal")
        reserved = {
            "run_id",
            "requirements",
            "repo_full_name",
            "blueprint",
            "trigger_actor",
            "principal",
            "team_id",
            "trace_id",
        }
        task = DevAITask(
            id=str(state.get("run_id") or ""),
            intent=str(state.get("requirements") or ""),
            repo=str(state.get("repo_full_name") or ""),
            blueprint=str(state.get("blueprint") or ""),
            triggered_by=str(state.get("trigger_actor") or ""),
            principal=dict(principal) if isinstance(principal, dict) else None,
            team_id=str(state.get("team_id") or ""),
            trace_id=str(state.get("trace_id") or ""),
        )
        # Everything else (stage_config, agent_profile, mcp_endpoints, …) becomes
        # handover context the spec can read via its declared context_keys.
        for key, value in state.items():
            if key not in reserved:
                task.agent_context[key] = value
        return task


__all__ = ["SpecializationService"]
