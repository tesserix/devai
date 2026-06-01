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
from devai.specializations.loader import load_specialization_from_string

if TYPE_CHECKING:
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
    ) -> None:
        self._store = store
        self._spec_registry = spec_registry

    # ── Specializations (agents) ──────────────────────────────────────

    async def create_specialization(self, yaml_text: str, created_by: str = "operator") -> dict[str, Any]:
        """Validate YAML, persist it, and register the agent live."""
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
        return {"name": spec.name, "display_name": spec.display_name, "category": spec.category}

    async def create_specialization_from_fields(self, fields: dict[str, Any], created_by: str = "operator") -> dict[str, Any]:
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
        logger.info("authored blueprint %s (by %s)", bp.name, created_by)
        return {"name": bp.name, "stages": len(getattr(bp, "stages", []) or [])}

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
        if self._spec_registry is None:
            return 0
        count = 0
        for defn in await self._store.list("specialization"):
            try:
                spec = load_specialization_from_string(defn.yaml, source=f"<authored:{defn.name}>")
                self._spec_registry.register_or_replace(spec)
                count += 1
            except Exception:  # noqa: BLE001
                logger.warning("authored spec %s failed to reload — skipping", defn.name, exc_info=True)
        if count:
            logger.info("authoring: re-registered %d stored agent(s) into the live registry", count)
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
    return yaml.safe_dump(doc, sort_keys=False)


__all__ = ["AuthoringService", "AuthoringError"]
