"""Blueprint runtime — YAML DAGs of stages.

A blueprint is a YAML file in `blueprints/` declaring an ordered (or
parallel) set of pipeline stages with dependencies, conditions, timeouts,
and per-stage config. The executor topo-sorts the DAG and runs each stage
through the stage registry.

Public surface:
    Blueprint, StageSpec       — the parsed YAML model
    load_blueprint             — file path → Blueprint
    BlueprintExecutor          — runs a Blueprint against a DevAITask
    StageRegistry              — maps stage keys to factory functions
    register_defaults          — wires in all built-in DevAI stages
"""

from devai.blueprint.executor import BlueprintExecutor
from devai.blueprint.loader import (
    Blueprint,
    BlueprintLoadError,
    StageSpec,
    discover_blueprints,
    load_blueprint,
    load_blueprint_from_string,
)
from devai.blueprint.registry import StageRegistry, register_defaults

__all__ = [
    "Blueprint",
    "BlueprintExecutor",
    "BlueprintLoadError",
    "StageRegistry",
    "StageSpec",
    "discover_blueprints",
    "load_blueprint",
    "load_blueprint_from_string",
    "register_defaults",
]
