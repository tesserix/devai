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
            # Build a registry-backed view layered over the local YAML.
            # Local seeds carry the runtime metadata (tools, handover
            # schema, context keys) that aregistry's slim schema drops;
            # aregistry is the authority for "what skills exist". We
            # use the catalog list to validate + augment but never to
            # OVERWRITE the runtime metadata.
            cat_skills = self._registry_client.list_skills()
            cat_agents = self._registry_client.list_agents()
            self._source = "registry+local" if (cat_skills or cat_agents) else "local"
            logger.info(
                "specializations: loaded %d local specs; aregistry advertises %d skills + %d agents (source=%s)",
                len(local_registry),
                len(cat_skills),
                len(cat_agents),
                self._source,
            )
        except Exception:  # noqa: BLE001
            logger.exception("specializations: aregistry consultation failed — falling back to local")
            self._source = "local"
        return local_registry

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
        """Re-discover from disk. Returns the new count.

        Used by the dashboard's "reload specs" action (and a future
        SIGHUP handler) so YAML edits take effect without a restart.
        """
        new_registry = SpecializationRegistry.from_directory(self.directory)
        self._registry = new_registry
        return len(new_registry)

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


__all__ = ["SpecializationService"]
