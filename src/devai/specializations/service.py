"""SpecializationService — FastAPI lifecycle wrapper for the registry.

Mirrors PipelineService. Holds one SpecializationRegistry, loaded from
`settings.specializations_dir`. Stored on `app.state.specialization_service`.

The loaded names form the immutable reviewed admission set for that reload.
Runtime authoring may add catalog objects for sandboxing, but it cannot expand
governed execution reachability.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
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


class GovernedAgentError(Exception):
    """A governed agent cannot be admitted for execution."""


class AgentNotAdmittedError(GovernedAgentError):
    """The requested capability has no reviewed local admission mapping."""


class AgentUnavailableError(GovernedAgentError):
    """The reviewed agent bundle is unavailable or invalid."""


@dataclass(slots=True, frozen=True)
class GovernedAgent:
    capability: str
    agent_name: str
    agent_version: str
    spec: Specialization
    resolved: Any

    def snapshot(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "agent": self.agent_name,
            "version": self.agent_version,
            "skills": _resolved_names(self.resolved.resolved.get("skills", [])),
            "tools": _resolved_names(self.resolved.resolved.get("tools", [])),
            "mcpServers": _resolved_names(self.resolved.resolved.get("mcpServers", [])),
            "prompts": _resolved_names(self.resolved.resolved.get("prompts", [])),
        }


class SpecializationService:
    def __init__(
        self,
        config: Settings,
        *,
        directory: str | Path | None = None,
        registry_client: Any | None = None,
    ) -> None:
        self.config = config
        configured_directory = directory or str(
            getattr(config, "specializations_dir", "specializations") or "specializations"
        )
        self.directory = Path(configured_directory)
        # Local YAML defines reviewed admission. Registry supplies the
        # deployment composition that every governed invocation validates.
        self._registry_client = registry_client
        self._registry: SpecializationRegistry | None = None
        self._reviewed_capabilities: frozenset[str] = frozenset()
        self._started = False
        self._source = "local"  # exposed via list_all() for the dashboard

    async def start(self) -> None:
        if self._started:
            return
        registry = await self._load()
        self._registry = registry
        self._started = True

    async def _load(self) -> SpecializationRegistry:
        """Load the reviewed local admission catalog.

        Registry artifacts describe deployed composition, but publication is
        not authorization to execute. Governed resolution later requires an
        exact Registry bundle for one of these locally reviewed capabilities.
        """
        local_registry = SpecializationRegistry()
        try:
            local_registry = SpecializationRegistry.from_directory(self.directory)
        except Exception:
            logger.exception("specializations: local YAML load failed — starting with empty")
        self._source = "local"
        self._reviewed_capabilities = frozenset(spec.name for spec in local_registry.all())
        logger.info(
            "specializations: loaded %d reviewed specs (source=local YAML at %s)",
            len(local_registry),
            self.directory,
        )
        return local_registry

    @property
    def source(self) -> str:
        """The reviewed admission catalog source, currently ``local``."""
        return self._source

    async def stop(self) -> None:
        self._started = False
        self._registry = None
        self._reviewed_capabilities = frozenset()

    async def reload(self) -> int:
        """Reload the reviewed disk catalog and return its new count."""
        new_registry = await self._load()
        self._registry = new_registry
        return len(new_registry)

    async def resolve_runnable(self, name: str) -> Specialization | None:
        """Return an admitted role after fresh Registry composition resolution."""
        try:
            return (await self.resolve_governed(name)).spec
        except AgentNotAdmittedError:
            return None

    async def resolve_governed(self, capability: str) -> GovernedAgent:
        normalized, _ = self._reviewed_spec(capability)
        if self._registry_client is None:
            raise AgentUnavailableError("registry is not configured")
        agent_name = f"{normalized.replace('_', '-')}-agent"
        try:
            resolved = await asyncio.to_thread(self._registry_client.resolve_agent, agent_name)
        except Exception as exc:  # noqa: BLE001 -- dependency boundary is translated and redacted
            logger.warning(
                "governed agent resolution failed agent=%s error_type=%s",
                agent_name,
                type(exc).__name__,
            )
            raise AgentUnavailableError("registry resolution failed") from None

        return self.admit_resolved(normalized, resolved)

    def _reviewed_spec(self, capability: str) -> tuple[str, Specialization]:
        normalized = capability.strip().lower().replace("-", "_").removesuffix("_agent")
        if normalized not in self._reviewed_capabilities:
            raise AgentNotAdmittedError("capability is not admitted")
        spec = self.get_full(normalized)
        if spec is None:
            raise AgentNotAdmittedError("capability is not admitted")
        if not bool(getattr(self.config, "llm_gateway_required", False)):
            raise AgentUnavailableError("mandatory LLM gateway routing is disabled")
        if not str(getattr(self.config, "llm_gateway_base_url", "") or "").strip():
            raise AgentUnavailableError("mandatory LLM gateway is not configured")
        return normalized, spec

    def admit_resolved(self, capability: str, resolved: Any) -> GovernedAgent:
        """Admit a dispatcher-resolved snapshot without another Registry read."""
        normalized, spec = self._reviewed_spec(capability)
        agent_name = f"{normalized.replace('_', '-')}-agent"
        self._validate_governed_bundle(spec, agent_name, resolved)
        if resolved.resolved.get("mcpServers") and not str(getattr(self.config, "agentgateway_url", "") or "").strip():
            raise AgentUnavailableError("mandatory MCP gateway is not configured")
        return GovernedAgent(
            capability=normalized,
            agent_name=agent_name,
            agent_version=resolved.agent.version,
            spec=spec,
            resolved=resolved,
        )

    @staticmethod
    def _validate_governed_bundle(spec: Specialization, agent_name: str, resolved: Any) -> None:
        agent = resolved.agent
        if agent.name != agent_name:
            raise AgentUnavailableError("registry returned a different agent")
        if resolved.unresolved:
            raise AgentUnavailableError("agent composition has unresolved references")

        expected_capability = spec.name.replace("_", "-")
        expected_prompt = f"{expected_capability}-prompt-v1"
        expected_version = str(spec.metadata.get("version") or "1.0.0")
        if spec.runtime.value != "tesserix_adk":
            raise AgentUnavailableError("local agent runtime is not admitted")
        required_labels = {
            "devai.io/source": "devai",
            "devai.io/risk-level": spec.risk_level.value,
            "ai.tesserix.dev/runtime": "tesserix-adk",
            "ai.tesserix.dev/provider-policy": "user-connectors",
        }
        if any(agent.labels.get(key) != value for key, value in required_labels.items()):
            raise AgentUnavailableError("agent policy labels are not admitted")
        if agent.version != expected_version:
            raise AgentUnavailableError("agent version is not admitted")
        if agent.model_provider != "devai-user-routing" or agent.model_name != "dynamic":
            raise AgentUnavailableError("agent model routing policy is not admitted")
        if agent.skills != [expected_capability] or agent.prompts != [expected_prompt]:
            raise AgentUnavailableError("agent references are not admitted")
        if not all(isinstance(ref, str) for ref in [*agent.tools, *agent.mcp_servers]):
            raise AgentUnavailableError("inline tool or MCP references are not admitted")

        skills = resolved.resolved.get("skills", [])
        prompts = resolved.resolved.get("prompts", [])
        if len(skills) != 1 or len(prompts) != 1:
            raise AgentUnavailableError("agent composition is incomplete")
        skill_meta, skill_spec = _object_parts(skills[0], "Skill", expected_capability)
        prompt_meta, prompt_spec = _object_parts(prompts[0], "Prompt", expected_prompt)

        expected_handover = {
            name: {
                "type": field.type,
                "required": field.required,
                "description": field.description,
            }
            for name, field in spec.handover_schema.items()
        }
        expected_skill = {
            "category": spec.category,
            "tools": list(spec.allowed_tools),
            "contextKeys": list(spec.context_keys),
            "outputKey": spec.output_key,
        }
        if any(skill_spec.get(key) != value for key, value in expected_skill.items()):
            raise AgentUnavailableError("resolved skill contract is not admitted")
        if _normalized_handover(skill_spec.get("handoverSchema")) != expected_handover:
            raise AgentUnavailableError("resolved skill contract is not admitted")
        skill_labels = skill_meta.get("labels")
        if not isinstance(skill_labels, dict) or skill_labels.get("devai.io/risk-level") != spec.risk_level.value:
            raise AgentUnavailableError("resolved skill risk policy is not admitted")

        expected_prompt_hash = hashlib.sha256(spec.system_prompt.encode("utf-8")).hexdigest()[:12]
        prompt_labels = prompt_meta.get("labels")
        if not isinstance(prompt_labels, dict) or prompt_labels.get("devai.io/prompt-hash") != expected_prompt_hash:
            raise AgentUnavailableError("resolved prompt hash is not admitted")
        if (
            prompt_spec.get("systemPrompt") != spec.system_prompt
            or prompt_spec.get("userPromptTemplate") != spec.user_prompt_template
        ):
            raise AgentUnavailableError("resolved prompt contract is not admitted")

        direct_tools = _resolved_names(resolved.resolved.get("tools", []))
        if direct_tools != agent.tools:
            raise AgentUnavailableError("resolved tools do not match agent references")
        if any(tool not in spec.allowed_tools for tool in direct_tools):
            raise AgentUnavailableError("resolved tool is not admitted")
        if _resolved_names(resolved.resolved.get("mcpServers", [])) != agent.mcp_servers:
            raise AgentUnavailableError("resolved MCP servers do not match agent references")

    # ── Read surface ─────────────────────────────────────────────────

    @property
    def registry(self) -> SpecializationRegistry:
        if self._registry is None:
            return SpecializationRegistry()
        return self._registry

    def admitted_specs(self) -> list[Specialization]:
        """Specs `resolve_governed` will admit — what A2A may advertise."""
        return [spec for spec in self.registry.all() if spec.name in self._reviewed_capabilities]

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
        resolved: Any = None,
    ) -> dict[str, Any] | None:
        """Run a YAML specialization through the Agent SDK and return its patch.

        This is the method the Job runner's entrypoint calls. It handles
        **YAML-only** specs (no ``legacy_python_class``) by dispatching a
        ``SpecAgent`` — the same unified runner the in-process pipeline uses, so
        a YAML role behaves identically as a Job or inline. (Before this, a
        YAML-only role could not run as a Job at all — the entrypoint's only
        fallback was reflection-loading a ``devai.agents.<name>`` Python class.)

        Non-governed local mode returns ``None`` for unknown or legacy Python
        roles. Mandatory-gateway mode performs fresh Registry admission and
        raises instead of falling back.

        ``deps`` is for tests; in production a minimal ``StageDeps`` (LLM + SCM
        built from ``config``, no Redis) is constructed.
        """
        if bool(getattr(self.config, "llm_gateway_required", False)):
            bundle = (
                self.admit_resolved(agent_name, resolved)
                if resolved is not None
                else await self.resolve_governed(agent_name)
            )
            return await self.invoke_bundle(bundle, state, deps=deps)

        spec = self.get_full(agent_name)
        if spec is None or spec.uses_legacy_runtime:
            return None

        return await self._invoke_spec(spec, state, deps=deps)

    async def invoke_bundle(
        self,
        bundle: GovernedAgent,
        state: dict[str, Any],
        *,
        deps: Any = None,
    ) -> dict[str, Any] | None:
        if bundle.spec.uses_legacy_runtime:
            return None
        return await self._invoke_spec(bundle.spec, state, deps=deps)

    async def invoke_spec(
        self,
        spec: Specialization,
        state: dict[str, Any],
        *,
        deps: Any = None,
    ) -> dict[str, Any] | None:
        """Run an already-admitted spec (e.g. an eval-gated registry agent).

        Callers own admission: this bypasses the reviewed local catalog, so it
        must only receive specs whose registry record passed the eval gate.
        """
        if spec.uses_legacy_runtime:
            return None
        return await self._invoke_spec(spec, state, deps=deps)

    async def _invoke_spec(
        self,
        spec: Specialization,
        state: dict[str, Any],
        *,
        deps: Any,
    ) -> dict[str, Any]:
        run_deps = deps if deps is not None else self._build_runner_deps()
        llm = getattr(run_deps, "llm", None)
        if bool(getattr(self.config, "llm_gateway_required", False)) and (
            llm is None or getattr(llm, "provider_name", "") == "noop"
        ):
            raise AgentUnavailableError("governed model adapter is unavailable")
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

        Construction stays defensive for non-governed local use. Governed
        invocation rejects a missing or noop adapter before ADK dispatch."""
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


def _object_parts(
    value: dict[str, Any], expected_kind: str, expected_name: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    metadata = value.get("metadata")
    spec = value.get("spec")
    if (
        value.get("kind") != expected_kind
        or not isinstance(metadata, dict)
        or metadata.get("name") != expected_name
        or not isinstance(spec, dict)
    ):
        raise AgentUnavailableError("resolved artifact identity is not admitted")
    return metadata, spec


def _resolved_names(values: list[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    for value in values:
        metadata = value.get("metadata")
        if not isinstance(metadata, dict) or not isinstance(metadata.get("name"), str):
            raise AgentUnavailableError("resolved artifact has no stable identity")
        names.append(metadata["name"])
    return names


def _normalized_handover(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        raise AgentUnavailableError("resolved handover contract is malformed")
    normalized: dict[str, dict[str, Any]] = {}
    for name, field in value.items():
        if not isinstance(name, str):
            raise AgentUnavailableError("resolved handover contract is malformed")
        if isinstance(field, str):
            normalized[name] = {"type": field, "required": True, "description": ""}
            continue
        if not isinstance(field, dict):
            raise AgentUnavailableError("resolved handover contract is malformed")
        field_type = field.get("type", "any")
        required = field.get("required", True)
        description = field.get("description", "")
        if not isinstance(field_type, str) or not isinstance(required, bool) or not isinstance(description, str):
            raise AgentUnavailableError("resolved handover contract is malformed")
        normalized[name] = {
            "type": field_type,
            "required": required,
            "description": description,
        }
    return normalized


__all__ = [
    "AgentNotAdmittedError",
    "AgentUnavailableError",
    "GovernedAgent",
    "GovernedAgentError",
    "SpecializationService",
]
