"""SpecializationRegistry — name → Specialization map.

Built once per process from `discover_specializations(dir)`. The Pipeline
service holds one and hands it to stages that need to look up a role
by name.
"""

from __future__ import annotations

import logging
from pathlib import Path

from devai.specializations.base import Specialization
from devai.specializations.loader import discover_specializations

logger = logging.getLogger(__name__)


class SpecializationRegistryError(Exception):
    """Raised on name conflicts or missing specs."""


class SpecializationRegistry:
    """Holds the loaded specs and answers name → spec lookups.

    Two construction paths:
      - `SpecializationRegistry()` + `.register(spec)` — programmatic
      - `SpecializationRegistry.from_directory(path)` — load all YAMLs

    The Pipeline uses `from_directory` at startup and re-discovers on
    a SIGHUP / dashboard "reload" action.
    """

    def __init__(self) -> None:
        self._by_name: dict[str, Specialization] = {}

    # ── Construction helpers ─────────────────────────────────────────

    @classmethod
    def from_directory(cls, directory: str | Path) -> SpecializationRegistry:
        registry = cls()
        for spec in discover_specializations(directory).values():
            registry.register(spec)
        return registry

    def register(self, spec: Specialization) -> None:
        if spec.name in self._by_name:
            raise SpecializationRegistryError(
                f"specialization {spec.name!r} already registered"
            )
        self._by_name[spec.name] = spec

    def register_or_replace(self, spec: Specialization) -> None:
        self._by_name[spec.name] = spec

    # ── Lookups ───────────────────────────────────────────────────────

    def resolve(self, name: str) -> Specialization:
        if name not in self._by_name:
            known = ", ".join(sorted(self._by_name.keys()))
            raise SpecializationRegistryError(
                f"unknown specialization {name!r}. Known: {known}"
            )
        return self._by_name[name]

    def has(self, name: str) -> bool:
        return name in self._by_name

    def known(self) -> list[str]:
        return sorted(self._by_name.keys())

    def by_category(self, category: str) -> list[Specialization]:
        return [s for s in self._by_name.values() if s.category == category]

    def all(self) -> list[Specialization]:
        return list(self._by_name.values())

    def __len__(self) -> int:
        return len(self._by_name)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._by_name


__all__ = ["SpecializationRegistry", "SpecializationRegistryError"]
