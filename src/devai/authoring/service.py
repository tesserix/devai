"""AuthoringService — create/list/delete user-authored agents & blueprints.

Validation always goes through the existing strict loaders
(:func:`load_specialization_from_string`, :func:`load_blueprint_from_string`)
so authored definitions obey exactly the same schema as the YAML on disk.
Persistence goes through a :class:`DefinitionStore`. When a
:class:`SpecializationRegistry` is supplied, newly-created agents are
registered live so they're runnable immediately (no reload).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import yaml

from devai.authoring.store import AuthoredDefinition, DefinitionStore
from devai.blueprint.loader import load_blueprint_from_string
from devai.registry import mapping
from devai.specializations.loader import load_specialization_from_string

if TYPE_CHECKING:
    from devai.config import Settings
    from devai.registry.client import RegistryClient
    from devai.specializations.registry import SpecializationRegistry

logger = logging.getLogger(__name__)


class AuthoringError(ValueError):
    """Raised when an authored definition fails validation."""


class AuthoringService:
    def __init__(
        self,
        store: DefinitionStore,
        *,
        spec_registry: SpecializationRegistry | None = None,
        registry_client: RegistryClient | None = None,
        settings: Settings | None = None,
        pipeline: Any | None = None,
    ) -> None:
        self._store = store
        self._spec_registry = spec_registry
        self._registry = registry_client
        self._settings = settings
        # Duck-typed PipelineService (has register_blueprint(bp)); when set,
        # authored blueprints become runnable the instant they're created.
        self._pipeline = pipeline

    # ── Publish helpers (best-effort; never break the local 201) ──────

    @property
    def _publish_enabled(self) -> bool:
        return bool(
            self._registry is not None
            and self._settings is not None
            and getattr(self._settings, "registry_publish_enabled", False)
        )

    @property
    def _tenant(self) -> str:
        return getattr(self._settings, "registry_default_tenant", "devai") or "devai"

    def _publish(self, kind: str, publisher, *args, **kwargs) -> None:
        """Run a registry publish, swallowing every error.

        Publishing to the shared catalog is a convenience, not a
        correctness requirement: the artifact is already persisted and
        hot-registered locally, so a registry outage must never turn a
        valid authoring request into a 5xx.
        """
        if not self._publish_enabled:
            return
        try:
            publisher(*args, **kwargs)
            logger.info("registry: published %s to shared catalog", kind)
        except Exception:  # noqa: BLE001
            logger.warning("registry: publish of %s failed (kept local copy)", kind, exc_info=True)

    # ── Specializations (agents) ──────────────────────────────────────

    async def create_specialization(self, yaml_text: str, created_by: str = "operator") -> dict[str, Any]:
        """Validate YAML, persist it, register live, and publish to the registry."""
        try:
            spec = load_specialization_from_string(yaml_text, source="<authored>")
        except Exception as e:  # noqa: BLE001 — surface a clean 422 to the caller
            raise AuthoringError(f"invalid specialization: {e}") from e

        await self._store.upsert(
            AuthoredDefinition(kind="specialization", name=spec.name, yaml=yaml_text, created_by=created_by)
        )
        if self._spec_registry is not None:
            self._spec_registry.register_or_replace(spec)
        logger.info("authored specialization %s (by %s)", spec.name, created_by)

        if self._publish_enabled:
            env = mapping.spec_to_agent_envelope(spec, tenant=self._tenant, created_by=created_by)
            self._publish(f"agent/{spec.name}", self._registry.publish_agent, env)
        return {"name": spec.name, "display_name": spec.display_name, "category": spec.category}

    async def create_specialization_from_fields(
        self, fields: dict[str, Any], created_by: str = "operator"
    ) -> dict[str, Any]:
        """Build a spec YAML from the Create-Agent form fields, then create it."""
        return await self.create_specialization(_spec_yaml_from_fields(fields), created_by=created_by)

    async def list_specializations(self) -> list[dict[str, Any]]:
        return [d.to_dict() for d in await self._store.list("specialization")]

    async def get_specialization(self, name: str) -> dict[str, Any] | None:
        d = await self._store.get("specialization", name)
        return d.to_dict() if d else None

    async def delete_specialization(self, name: str) -> bool:
        return await self._store.delete("specialization", name)

    # ── Blueprints (workflows) ────────────────────────────────────────

    async def create_blueprint(self, yaml_text: str, created_by: str = "operator") -> dict[str, Any]:
        try:
            bp = load_blueprint_from_string(yaml_text, source="<authored>")
        except Exception as e:  # noqa: BLE001
            raise AuthoringError(f"invalid blueprint: {e}") from e
        await self._store.upsert(
            AuthoredDefinition(kind="blueprint", name=bp.name, yaml=yaml_text, created_by=created_by)
        )
        # Hot-register into the live pipeline so it's runnable immediately.
        if self._pipeline is not None:
            self._pipeline.register_blueprint(bp)
        logger.info("authored blueprint %s (by %s)", bp.name, created_by)

        if self._publish_enabled:
            env = mapping.blueprint_to_blueprint_envelope(bp, yaml_text, tenant=self._tenant, created_by=created_by)
            self._publish(f"blueprint/{bp.name}", self._registry.publish_blueprint, env)
        return {"name": bp.name, "stages": len(getattr(bp, "stages", []) or [])}

    # ── Skills + custom tools (MCP servers) ───────────────────────────

    async def create_skill(self, fields: dict[str, Any], created_by: str = "operator") -> dict[str, Any]:
        """Persist a user-authored Skill and publish it to the registry.

        Skills have no devai-local runtime form (they're catalog metadata the
        runner resolves at run-time), so the canonical copy lives as the
        registry envelope stashed in the store's ``yaml`` slot.
        """
        name = (fields.get("name") or "").strip()
        if not name:
            raise AuthoringError("skill: 'name' is required")
        env = mapping.skill_fields_to_skill_envelope(fields, tenant=self._tenant, created_by=created_by)
        await self._store.upsert(
            AuthoredDefinition(
                kind="skill", name=name, yaml=yaml.safe_dump(env, sort_keys=False), created_by=created_by
            )
        )
        logger.info("authored skill %s (by %s)", name, created_by)
        if self._publish_enabled:
            self._publish(f"skill/{name}", self._registry.publish_skill, env)
        return {"name": name, "title": env["spec"].get("title", name)}

    async def create_mcp_server(self, fields: dict[str, Any], created_by: str = "operator") -> dict[str, Any]:
        """Persist a user-registered MCP server (custom tool) + publish it."""
        name = (fields.get("name") or "").strip()
        if not name:
            raise AuthoringError("mcp_server: 'name' is required")
        env = mapping.mcp_fields_to_mcpserver_envelope(fields, tenant=self._tenant, created_by=created_by)
        await self._store.upsert(
            AuthoredDefinition(
                kind="mcp_server", name=name, yaml=yaml.safe_dump(env, sort_keys=False), created_by=created_by
            )
        )
        logger.info("authored mcp_server %s (by %s)", name, created_by)
        if self._publish_enabled:
            self._publish(f"mcpserver/{name}", self._registry.publish_mcp_server, env)
        return {"name": name, "type": env["spec"].get("type", "remote")}

    async def list_skills(self) -> list[dict[str, Any]]:
        return [d.to_dict() for d in await self._store.list("skill")]

    async def list_mcp_servers(self) -> list[dict[str, Any]]:
        return [d.to_dict() for d in await self._store.list("mcp_server")]

    async def delete_skill(self, name: str) -> bool:
        return await self._store.delete("skill", name)

    async def delete_mcp_server(self, name: str) -> bool:
        return await self._store.delete("mcp_server", name)

    async def republish_all(self) -> int:
        """Re-publish every stored authored artifact to the registry.

        Reconciles the shared catalog after a registry wipe or a devai
        redeploy. Best-effort per-item; returns the count attempted.
        """
        if not self._publish_enabled:
            return 0
        count = 0
        for defn in await self._store.list("specialization"):
            try:
                spec = load_specialization_from_string(defn.yaml, source=f"<authored:{defn.name}>")
                env = mapping.spec_to_agent_envelope(spec, tenant=self._tenant, created_by=defn.created_by)
                self._publish(f"agent/{defn.name}", self._registry.publish_agent, env)
                count += 1
            except Exception:  # noqa: BLE001
                logger.warning("republish: agent %s failed", defn.name, exc_info=True)
        for defn in await self._store.list("blueprint"):
            try:
                bp = load_blueprint_from_string(defn.yaml, source=f"<authored:{defn.name}>")
                env = mapping.blueprint_to_blueprint_envelope(
                    bp, defn.yaml, tenant=self._tenant, created_by=defn.created_by
                )
                self._publish(f"blueprint/{defn.name}", self._registry.publish_blueprint, env)
                count += 1
            except Exception:  # noqa: BLE001
                logger.warning("republish: blueprint %s failed", defn.name, exc_info=True)
        for kind, plural in (("skill", "publish_skill"), ("mcp_server", "publish_mcp_server")):
            for defn in await self._store.list(kind):
                try:
                    env = yaml.safe_load(defn.yaml)
                    self._publish(f"{kind}/{defn.name}", getattr(self._registry, plural), env)
                    count += 1
                except Exception:  # noqa: BLE001
                    logger.warning("republish: %s %s failed", kind, defn.name, exc_info=True)
        if count:
            logger.info("registry: republished %d authored artifact(s)", count)
        return count

    async def list_blueprints(self) -> list[dict[str, Any]]:
        return [d.to_dict() for d in await self._store.list("blueprint")]

    async def get_blueprint(self, name: str) -> dict[str, Any] | None:
        d = await self._store.get("blueprint", name)
        return d.to_dict() if d else None

    async def delete_blueprint(self, name: str) -> bool:
        return await self._store.delete("blueprint", name)

    # ── Boot ──────────────────────────────────────────────────────────

    async def load_into_registry(self) -> int:
        """Re-register all stored agents into the live registry (call on boot).

        Skips any that no longer validate (e.g. a tool was removed) rather
        than failing startup. Returns the number registered.
        """
        count = 0
        if self._spec_registry is not None:
            for defn in await self._store.list("specialization"):
                try:
                    spec = load_specialization_from_string(defn.yaml, source=f"<authored:{defn.name}>")
                    self._spec_registry.register_or_replace(spec)
                    count += 1
                except Exception:  # noqa: BLE001
                    logger.warning("authored spec %s failed to reload — skipping", defn.name, exc_info=True)
        if self._pipeline is not None:
            for defn in await self._store.list("blueprint"):
                try:
                    bp = load_blueprint_from_string(defn.yaml, source=f"<authored:{defn.name}>")
                    if self._pipeline.register_blueprint(bp):
                        count += 1
                except Exception:  # noqa: BLE001
                    logger.warning("authored blueprint %s failed to reload — skipping", defn.name, exc_info=True)
        if count:
            logger.info("authoring: re-registered %d stored artifact(s) into the live runtime", count)
        return count


def _spec_yaml_from_fields(fields: dict[str, Any]) -> str:
    """Translate Create-Agent form fields into a specialization YAML doc."""
    doc: dict[str, Any] = {
        "name": fields["name"],
        "display_name": fields.get("display_name", "") or fields["name"],
        "description": fields.get("description", ""),
        "category": fields.get("category", "specialist"),
        "llm_provider": fields.get("llm_provider", "auto"),
        "system_prompt": fields.get("system_prompt", ""),
        "allowed_tools": list(fields.get("allowed_tools", []) or []),
    }
    if fields.get("llm_model"):
        doc["llm_model"] = fields["llm_model"]
    if fields.get("temperature") is not None:
        doc["temperature"] = fields["temperature"]
    if fields.get("risk_level"):
        doc["risk_level"] = fields["risk_level"]
    if fields.get("output_key"):
        doc["output_key"] = fields["output_key"]
    handover = fields.get("handover_schema")
    if isinstance(handover, dict) and handover:
        doc["handover_schema"] = handover

    # Registry-composition picks (skills/prompts/MCP servers chosen from the
    # catalog) ride along on the spec's free-form metadata. The loader passes
    # metadata through untouched, and mapping.spec_to_agent_envelope reads
    # these keys to build the published Agent's ref lists.
    metadata: dict[str, Any] = dict(fields.get("metadata") or {})
    for form_key, md_key in (
        ("skills", "registry_skills"),
        ("prompts", "registry_prompts"),
        ("mcp_servers", "registry_mcp_servers"),
        ("registry_tools", "registry_tools"),
    ):
        vals = fields.get(form_key)
        if vals:
            metadata[md_key] = list(vals)
    if isinstance(fields.get("a2a"), dict) and fields["a2a"]:
        metadata["a2a"] = fields["a2a"]
    if metadata:
        doc["metadata"] = metadata
    return yaml.safe_dump(doc, sort_keys=False)


__all__ = ["AuthoringService", "AuthoringError"]
