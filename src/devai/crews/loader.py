"""Seed-crew loader — reads the ready-made crews shipped in `crews/*.yaml`.

Mirrors the specialization/blueprint loaders: discover `.yaml` files in a
directory, parse each into a `CrewSpec`, skip (with a warning) any that fail
validation. Team-created crews live in the DB instead; both are unified by
`resolve_crew` in the crew runner stage.
"""

from __future__ import annotations

import logging
from pathlib import Path

from devai.crews.models import CrewSpec

logger = logging.getLogger(__name__)


def load_seed_crews(directory: str | Path) -> dict[str, CrewSpec]:
    """Load every `*.yaml` crew under `directory` → {name: CrewSpec}."""
    path = Path(directory)
    crews: dict[str, CrewSpec] = {}
    if not path.is_dir():
        return crews

    import yaml

    for yaml_path in sorted(path.glob("*.yaml")):
        try:
            with yaml_path.open("r", encoding="utf-8") as fh:
                raw = yaml.safe_load(fh) or {}
            crew = CrewSpec.from_dict(raw)
            if not crew.name:
                logger.warning("seed crew %s has no name — skipping", yaml_path)
                continue
            crews[crew.name] = crew
        except Exception:  # noqa: BLE001 — one bad seed shouldn't break the rest
            logger.exception("failed to load seed crew %s", yaml_path)
    return crews


def seed_crew_registry(directory: str | Path = "crews") -> dict[str, CrewSpec]:
    """Convenience wrapper used by the runtime; same as load_seed_crews."""
    return load_seed_crews(directory)


__all__ = ["load_seed_crews", "seed_crew_registry"]
