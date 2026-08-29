"""Specialization YAML → in-memory model.

Strict on shape, permissive on optional fields. Every unknown top-level
or handover-field key raises — typos in YAML are easy to miss, and we'd
rather break loudly at load time than silently ignore a misplaced flag.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import yaml

from devai.specializations.base import (
    AgentRuntime,
    HandoverField,
    LLMProvider,
    RiskLevel,
    Specialization,
)

logger = logging.getLogger(__name__)


class SpecializationLoadError(Exception):
    """Raised on any structural problem in a specialization YAML."""


_ALLOWED_TOP_KEYS = {
    "name",
    "display_name",
    "description",
    "category",
    "llm_provider",
    "llm_model",
    "temperature",
    "max_tokens",
    "system_prompt",
    "user_prompt_template",
    "allowed_tools",
    "max_turns",
    "timeout",
    "context_keys",
    "output_key",
    "handover_schema",
    "risk_level",
    "role_color",
    "runtime",
    "legacy_python_class",
    "metadata",
}

_ALLOWED_HANDOVER_KEYS = {"type", "required", "description"}

_DURATION_RE = re.compile(r"^\s*(\d+)\s*(ms|s|m|h)?\s*$")

# A user-authored spec may only bind `legacy_python_class:` to a curated set
# of in-tree agent modules. Without this, the authoring API (auth is off by
# default) is an importlib-of-arbitrary-module code-exec path in the
# orchestrator process. On-disk built-in specs are loaded with authored=False
# and bypass this restriction. The prefix is the package the runner imports
# legacy agents from; the explicit set is the allowlist within it.
_LEGACY_CLASS_ALLOWED_PREFIX = "devai.agents."
_LEGACY_CLASS_ALLOWLIST: frozenset[str] = frozenset()


def _legacy_class_allowed(dotted: str) -> bool:
    """True if a user-authored spec may bind this legacy_python_class.

    Allowed when it's on the explicit allowlist OR lives under the curated
    ``devai.agents.*`` package (a real, in-tree agent module — never an
    attacker-supplied top-level import like ``os`` or ``subprocess``). A
    module path under the prefix must still name a submodule (have a dotted
    tail), so the bare prefix alone is rejected.
    """
    if not dotted:
        return True  # empty == no legacy bridge; nothing to restrict
    if dotted in _LEGACY_CLASS_ALLOWLIST:
        return True
    return dotted.startswith(_LEGACY_CLASS_ALLOWED_PREFIX) and len(dotted) > len(_LEGACY_CLASS_ALLOWED_PREFIX)


def _parse_timeout(value: Any, *, default: int = 900) -> int:
    """Parse Fiber-style duration. Returns seconds (int)."""
    if value is None or value == "":
        return default
    if isinstance(value, int | float):
        return int(value)
    if not isinstance(value, str):
        raise SpecializationLoadError(f"timeout must be string or number, got {type(value).__name__}")
    match = _DURATION_RE.match(value)
    if not match:
        raise SpecializationLoadError(f"unparseable duration: {value!r}")
    num = int(match.group(1))
    unit = (match.group(2) or "s").lower()
    multiplier = {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unit]
    return int(num * multiplier)


def _parse_handover_schema(raw: Any, *, source: str) -> dict[str, HandoverField]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise SpecializationLoadError(f"{source}: handover_schema must be a mapping, got {type(raw).__name__}")
    out: dict[str, HandoverField] = {}
    for field_name, field_def in raw.items():
        if not isinstance(field_name, str) or not field_name:
            raise SpecializationLoadError(f"{source}: handover field name must be a non-empty string")

        if isinstance(field_def, str):
            # Shorthand: "branch_name: string" → required field of that type
            try:
                out[field_name] = HandoverField(name=field_name, type=field_def, required=True)
            except ValueError as e:
                raise SpecializationLoadError(f"{source}: {e}") from e
            continue

        if not isinstance(field_def, dict):
            raise SpecializationLoadError(f"{source}: handover field {field_name!r} must be a mapping or a type-string")

        unknown = set(field_def.keys()) - _ALLOWED_HANDOVER_KEYS
        if unknown:
            raise SpecializationLoadError(
                f"{source}: handover field {field_name!r} has unknown keys: {sorted(unknown)}"
            )

        try:
            out[field_name] = HandoverField(
                name=field_name,
                type=str(field_def.get("type", "any")),
                required=bool(field_def.get("required", True)),
                description=str(field_def.get("description", "")),
            )
        except ValueError as e:
            raise SpecializationLoadError(f"{source}: {e}") from e

    return out


def load_specialization_from_string(text: str, *, source: str = "<inline>", authored: bool = False) -> Specialization:
    """Parse a specialization YAML string into a Specialization.

    ``authored=True`` marks the input as coming from the user-authoring API
    (the dashboard Create-Agent form / raw YAML), which is subject to extra
    restrictions a trusted on-disk built-in spec is not — notably the
    ``legacy_python_class`` allowlist (see :func:`_legacy_class_allowed`).
    On-disk built-ins load with ``authored=False`` (the default), preserving
    today's behavior for the shipped catalog.
    """
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise SpecializationLoadError(f"{source}: invalid YAML: {e}") from e
    if not isinstance(raw, dict):
        raise SpecializationLoadError(f"{source}: top level must be a mapping, got {type(raw).__name__}")

    unknown = set(raw.keys()) - _ALLOWED_TOP_KEYS
    if unknown:
        raise SpecializationLoadError(f"{source}: unknown top-level keys: {sorted(unknown)}")

    name = raw.get("name")
    if not name or not isinstance(name, str):
        raise SpecializationLoadError(f"{source}: missing or invalid 'name'")

    legacy_class = str(raw.get("legacy_python_class", "") or "")
    if authored and not _legacy_class_allowed(legacy_class):
        raise SpecializationLoadError(
            f"{source}: legacy_python_class {legacy_class!r} is not permitted on an authored agent "
            f"(only built-in modules under {_LEGACY_CLASS_ALLOWED_PREFIX}* are allowed)"
        )

    system_prompt = raw.get("system_prompt", "") or ""
    if not isinstance(system_prompt, str):
        raise SpecializationLoadError(f"{source}: 'system_prompt' must be a string")

    allowed_tools = raw.get("allowed_tools") or []
    if not isinstance(allowed_tools, list) or not all(isinstance(t, str) for t in allowed_tools):
        raise SpecializationLoadError(f"{source}: 'allowed_tools' must be a list of strings")

    context_keys = raw.get("context_keys") or []
    if not isinstance(context_keys, list) or not all(isinstance(c, str) for c in context_keys):
        raise SpecializationLoadError(f"{source}: 'context_keys' must be a list of strings")

    metadata = raw.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise SpecializationLoadError(f"{source}: 'metadata' must be a mapping")

    try:
        spec = Specialization(
            name=str(name),
            display_name=str(raw.get("display_name", "")),
            description=str(raw.get("description", "")),
            category=str(raw.get("category", "specialist")),
            llm_provider=LLMProvider.parse(raw.get("llm_provider"), default=LLMProvider.AUTO),
            llm_model=str(raw.get("llm_model", "")),
            temperature=_parse_optional_float(raw.get("temperature"), "temperature", source),
            max_tokens=_parse_optional_int(raw.get("max_tokens"), "max_tokens", source),
            system_prompt=system_prompt,
            user_prompt_template=str(raw.get("user_prompt_template", "")),
            allowed_tools=list(allowed_tools),
            max_turns=int(raw.get("max_turns", 50)),
            timeout_seconds=_parse_timeout(raw.get("timeout"), default=900),
            context_keys=list(context_keys),
            output_key=str(raw.get("output_key", "")),
            handover_schema=_parse_handover_schema(raw.get("handover_schema"), source=source),
            risk_level=RiskLevel.parse(raw.get("risk_level"), default=RiskLevel.MEDIUM),
            role_color=str(raw.get("role_color", "engineer")),
            runtime=AgentRuntime.parse(raw.get("runtime")),
            legacy_python_class=legacy_class,
            metadata=dict(metadata),
        )
    except ValueError as e:
        raise SpecializationLoadError(f"{source}: {e}") from e

    return spec


def load_specialization(path: str | Path) -> Specialization:
    p = Path(path)
    if not p.exists():
        raise SpecializationLoadError(f"specialization not found: {p}")
    text = p.read_text(encoding="utf-8")
    spec = load_specialization_from_string(text, source=str(p))
    return spec


def discover_specializations(directory: str | Path) -> dict[str, Specialization]:
    """Walk `directory` recursively, load every `*.yaml` / `*.yml` it finds.

    Files starting with `_` are skipped — matches Fiber convention for
    base/abstract specs that aren't selectable on their own.
    """
    d = Path(directory)
    if not d.exists():
        return {}
    out: dict[str, Specialization] = {}
    for path in sorted(d.rglob("*.yaml")):
        if path.name.startswith("_"):
            continue
        spec = load_specialization(path)
        if spec.name in out:
            other = "(unknown)"
            for p, s in out.items():
                if s.name == spec.name:
                    other = p
                    break
            raise SpecializationLoadError(f"duplicate specialization name {spec.name!r} (in {path} and {other})")
        out[spec.name] = spec
    for path in sorted(d.rglob("*.yml")):
        if path.name.startswith("_"):
            continue
        spec = load_specialization(path)
        if spec.name in out:
            raise SpecializationLoadError(f"duplicate specialization {spec.name!r}: {path}")
        out[spec.name] = spec
    return out


# ──────────────────────────────────────────────────────────────────────
# Internal: optional numeric parsing
# ──────────────────────────────────────────────────────────────────────


def _parse_optional_float(value: Any, name: str, source: str) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as e:
        raise SpecializationLoadError(f"{source}: '{name}' must be a number, got {value!r}") from e


def _parse_optional_int(value: Any, name: str, source: str) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as e:
        raise SpecializationLoadError(f"{source}: '{name}' must be an int, got {value!r}") from e


__all__ = [
    "SpecializationLoadError",
    "discover_specializations",
    "load_specialization",
    "load_specialization_from_string",
]
