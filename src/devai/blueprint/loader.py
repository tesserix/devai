"""Blueprint YAML → in-memory model.

Mirrors the Fiber blueprint format. A blueprint is a YAML doc:

    name: alm-pipeline
    description: Full ALM pipeline (port of the 14-node LangGraph)
    stages:
      - name: ingest-documents
        type: deterministic
        stage: ingest_documents
      - name: analyze
        type: agentic
        stage: analyze_requirements
        depends_on: [ingest-documents]
        timeout: 15m
        config:
          provider: openai

Validation is strict on shape (no unknown top-level keys, every stage
declared with `name` + `stage`), permissive on missing depends_on for
the first stage. Cycle detection happens in BlueprintExecutor.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from devai.blueprint.conditions import _TASK_BOOL_KEYS, validate_condition

logger = logging.getLogger(__name__)

# Human-readable hint for the load-time condition error (the bare property
# names without the `task.` prefix), so the message points the author at the
# valid keys without exposing the internal frozenset directly.
_TASK_BOOL_KEYS_HINT = _TASK_BOOL_KEYS


class BlueprintLoadError(Exception):
    """Raised on any structural problem in the YAML."""


_DURATION_RE = re.compile(r"^\s*(\d+)\s*(ms|s|m|h)?\s*$")


def _parse_duration(value: Any, *, default: float | None = None) -> float | None:
    """Parse Fiber-style duration strings: `15m`, `30s`, `500ms`, `2h`.

    Bare numbers are treated as seconds (matches Fiber behavior).
    None / empty / missing → default.
    """
    if value is None or value == "":
        return default
    if isinstance(value, int | float):
        return float(value)
    if not isinstance(value, str):
        raise BlueprintLoadError(f"timeout must be string or number, got {type(value).__name__}")
    match = _DURATION_RE.match(value)
    if not match:
        raise BlueprintLoadError(f"unparseable duration: {value!r}")
    num = float(match.group(1))
    unit = (match.group(2) or "s").lower()
    multiplier = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}[unit]
    return num * multiplier


@dataclass(slots=True)
class StageSpec:
    """One entry in `blueprint.stages` — the parsed YAML node.

    `name` is the per-blueprint identifier (used in depends_on); `stage`
    is the registry key (the factory to instantiate). Two stages can share
    the same `stage:` key but must have distinct `name:` values, which is
    how a blueprint runs e.g. two security scans at different gates.
    """

    name: str
    stage: str
    type: str = "deterministic"  # deterministic | agentic | review | analyst
    depends_on: list[str] = field(default_factory=list)
    condition: str | None = None
    timeout_seconds: float | None = None
    # Transient-failure retries for this stage (exceptions/timeouts re-run
    # the stage with backoff before on_failure applies). 0 = use the global
    # default (config.pipeline_stage_retries). Stages are resume-idempotent
    # by design, so a retry is no riskier than a pod-restart resume.
    retries: int = 0
    on_failure: str = "stop"  # stop | rollback | continue
    config: dict[str, str] = field(default_factory=dict)
    parallel: bool = False  # marks this stage as part of a parallel level

    # ── Render metadata (all optional; powers the data-driven UI flow view) ──
    # These never affect execution — they only feed the blueprint-graph
    # endpoint so the dashboards can draw any blueprint without hardcoding
    # nodes. Sensible fallbacks mean existing blueprints render unchanged.
    title: str = ""  # display name; empty → humanized `name`
    lane: str = ""  # visual column/group (e.g. plan | build | review | deploy)
    color: str = ""  # UI color token; empty → UI default
    agent: str = ""  # specialization key this node runs; empty → config["agent"]
    gate: bool = False  # human-approval gate node

    def __post_init__(self) -> None:
        if self.on_failure not in {"stop", "rollback", "continue"}:
            raise BlueprintLoadError(
                f"stage {self.name!r}: on_failure must be stop|rollback|continue, got {self.on_failure!r}"
            )
        if self.type not in {"deterministic", "agentic", "review", "analyst", "deploy", "context"}:
            # We don't restrict the type strictly — it's informational for
            # the dashboard — but we still want to catch typos.
            logger.debug("stage %s has uncommon type %r", self.name, self.type)

    def display_title(self) -> str:
        """Render-friendly title — falls back to a humanized stage name."""
        if self.title:
            return self.title
        return self.name.replace("-", " ").replace("_", " ").strip().title()

    def resolved_agent(self) -> str:
        """Specialization key for this node. Explicit `agent:` wins; otherwise
        fall back to `config.agent` (how job-runner stages already name their
        agent), so nodes light up with the right agent without duplication."""
        return self.agent or self.config.get("agent", "")

    def to_dict(self) -> dict[str, Any]:
        """Flat, render-ready dict. Used by the blueprint-graph endpoint."""
        return {
            "name": self.name,
            "stage": self.stage,
            "type": self.type,
            "title": self.display_title(),
            "lane": self.lane,
            "color": self.color,
            "agent": self.resolved_agent(),
            "gate": self.gate,
            "depends_on": list(self.depends_on),
            "condition": self.condition,
            "timeout_seconds": self.timeout_seconds,
            "on_failure": self.on_failure,
            "parallel": self.parallel,
            "config": dict(self.config),
        }


@dataclass(slots=True)
class Blueprint:
    """A parsed blueprint, ready to hand to BlueprintExecutor."""

    name: str
    description: str
    stages: list[StageSpec]
    metadata: dict[str, Any] = field(default_factory=dict)

    def stage_by_name(self, name: str) -> StageSpec | None:
        for s in self.stages:
            if s.name == name:
                return s
        return None

    def stage_names(self) -> set[str]:
        return {s.name for s in self.stages}


# ──────────────────────────────────────────────────────────────────────────
# Loaders
# ──────────────────────────────────────────────────────────────────────────

_ALLOWED_TOP_KEYS = {"name", "description", "stages", "metadata"}
_ALLOWED_STAGE_KEYS = {
    "name",
    "type",
    "stage",
    "depends_on",
    "condition",
    "timeout",
    "on_failure",
    "config",
    "parallel",
    # ── render metadata (optional) ──
    "title",
    "lane",
    "color",
    "agent",
    "gate",
}


def load_blueprint_from_string(text: str, *, source: str = "<inline>") -> Blueprint:
    """Parse a YAML string into a Blueprint."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise BlueprintLoadError(f"{source}: invalid YAML: {e}") from e
    if not isinstance(raw, dict):
        raise BlueprintLoadError(f"{source}: top level must be a mapping, got {type(raw).__name__}")

    unknown = set(raw.keys()) - _ALLOWED_TOP_KEYS
    if unknown:
        raise BlueprintLoadError(f"{source}: unknown top-level keys: {sorted(unknown)}")

    name = raw.get("name")
    if not name or not isinstance(name, str):
        raise BlueprintLoadError(f"{source}: missing or invalid 'name'")

    description = raw.get("description", "")
    if not isinstance(description, str):
        raise BlueprintLoadError(f"{source}: 'description' must be a string")

    stages_raw = raw.get("stages")
    if not isinstance(stages_raw, list) or not stages_raw:
        raise BlueprintLoadError(f"{source}: 'stages' must be a non-empty list")

    stages: list[StageSpec] = []
    seen_names: set[str] = set()
    for i, entry in enumerate(stages_raw):
        if not isinstance(entry, dict):
            raise BlueprintLoadError(f"{source}: stages[{i}] must be a mapping")
        unknown_stage_keys = set(entry.keys()) - _ALLOWED_STAGE_KEYS
        if unknown_stage_keys:
            raise BlueprintLoadError(f"{source}: stages[{i}] has unknown keys: {sorted(unknown_stage_keys)}")

        stage_name = entry.get("name")
        stage_key = entry.get("stage")
        if not stage_name or not isinstance(stage_name, str):
            raise BlueprintLoadError(f"{source}: stages[{i}] missing 'name'")
        if not stage_key or not isinstance(stage_key, str):
            raise BlueprintLoadError(f"{source}: stages[{i}] ({stage_name}) missing 'stage'")
        if stage_name in seen_names:
            raise BlueprintLoadError(f"{source}: duplicate stage name {stage_name!r}")
        seen_names.add(stage_name)

        depends_on = entry.get("depends_on") or []
        if not isinstance(depends_on, list) or not all(isinstance(d, str) for d in depends_on):
            raise BlueprintLoadError(f"{source}: stages[{i}] ({stage_name}) depends_on must be a list of strings")

        # Validate the condition's keys at load time. A typo'd bare key (e.g.
        # `task.haspr`) would otherwise fail open at runtime and let a gated
        # stage run unconditionally — break loudly here instead. Prefixed
        # (output./state.) keys resolve dynamically and stay fail-open.
        condition = entry.get("condition")
        if condition is not None:
            if not isinstance(condition, str):
                raise BlueprintLoadError(f"{source}: stages[{i}] ({stage_name}) condition must be a string")
            unknown_keys = validate_condition(condition)
            if unknown_keys:
                raise BlueprintLoadError(
                    f"{source}: stages[{i}] ({stage_name}) condition references unknown key(s): "
                    f"{sorted(unknown_keys)}. Known task keys: {sorted(_TASK_BOOL_KEYS_HINT)} "
                    f"(or an output./state. prefix)"
                )

        config = entry.get("config") or {}
        if not isinstance(config, dict):
            raise BlueprintLoadError(f"{source}: stages[{i}] ({stage_name}) config must be a mapping")
        # Coerce values to strings — Fiber config maps are string→string.
        config = {str(k): str(v) for k, v in config.items()}

        stages.append(
            StageSpec(
                name=stage_name,
                stage=stage_key,
                type=str(entry.get("type", "deterministic")),
                depends_on=list(depends_on),
                condition=entry.get("condition"),
                timeout_seconds=_parse_duration(entry.get("timeout")),
                on_failure=str(entry.get("on_failure", "stop")),
                config=config,
                parallel=bool(entry.get("parallel", False)),
                title=str(entry.get("title", "")),
                lane=str(entry.get("lane", "")),
                color=str(entry.get("color", "")),
                agent=str(entry.get("agent", "")),
                gate=bool(entry.get("gate", False)),
                retries=int(entry.get("retries", 0) or 0),
            )
        )

    # Validate all depends_on references resolve.
    all_names = {s.name for s in stages}
    for s in stages:
        for dep in s.depends_on:
            if dep not in all_names:
                raise BlueprintLoadError(f"{source}: stage {s.name!r} depends on unknown stage {dep!r}")

    metadata = raw.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise BlueprintLoadError(f"{source}: 'metadata' must be a mapping")

    return Blueprint(name=name, description=description, stages=stages, metadata=metadata)


def load_blueprint(path: str | Path) -> Blueprint:
    """Read a blueprint YAML file and parse it."""
    p = Path(path)
    if not p.exists():
        raise BlueprintLoadError(f"blueprint file not found: {p}")
    text = p.read_text(encoding="utf-8")
    return load_blueprint_from_string(text, source=str(p))


def discover_blueprints(directory: str | Path) -> dict[str, Blueprint]:
    """Load every `*.yaml` / `*.yml` in `directory` into a {name: Blueprint} map.

    Files starting with `_` (e.g. `_review-base.yaml`) are skipped — matches
    Fiber convention for base/abstract blueprints.
    """
    d = Path(directory)
    if not d.exists():
        return {}
    out: dict[str, Blueprint] = {}
    for path in sorted(d.iterdir()):
        if path.is_dir():
            continue
        if path.name.startswith("_"):
            continue
        if path.suffix not in {".yaml", ".yml"}:
            continue
        bp = load_blueprint(path)
        if bp.name in out:
            raise BlueprintLoadError(f"duplicate blueprint name {bp.name!r} (in {path} and earlier file)")
        out[bp.name] = bp
    return out
