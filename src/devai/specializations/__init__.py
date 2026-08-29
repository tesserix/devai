"""Specializations — Fiber-style agent role personas as YAML.

A **specialization** is a YAML file that declares one agent role:

    name: senior_developer
    description: ...
    llm_provider: claude            # which provider this role prefers
    llm_model: claude-sonnet-4
    system_prompt: |
      You are a Senior Software Engineer ...
    allowed_tools: [file_tools, scm_tools, test_tools]
    max_turns: 120
    timeout: 30m
    context_keys: [requirements_analyst_output, engineering_manager_output]
    output_key: senior_developer_output
    handover_schema:
      branch_name: {type: string, required: true}
      pr_number: {type: integer, required: true}
      implementation_summary: {type: string, required: true}
      committed_files: {type: array, required: false}
    risk_level: medium              # low | medium | high | critical
    role_color: engineer            # for the dashboard

This module exposes the runtime that loads, registers, and validates specs.
Concrete YAMLs live in the `specializations/` directory at the repo root.

Public surface:
    Specialization, HandoverField   — in-memory shape
    load_specialization(path)        — single-file parse
    discover_specializations(dir)    — load every .yaml under a tree
    SpecializationRegistry           — name → spec map
    validate_handover(spec, data)    — handover_schema check
"""

from devai.specializations.base import (
    AgentRuntime,
    HandoverField,
    LLMProvider,
    RiskLevel,
    Specialization,
)
from devai.specializations.loader import (
    SpecializationLoadError,
    discover_specializations,
    load_specialization,
    load_specialization_from_string,
)
from devai.specializations.registry import SpecializationRegistry
from devai.specializations.validator import HandoverValidationError, validate_handover

__all__ = [
    "AgentRuntime",
    "HandoverField",
    "HandoverValidationError",
    "LLMProvider",
    "RiskLevel",
    "Specialization",
    "SpecializationLoadError",
    "SpecializationRegistry",
    "discover_specializations",
    "load_specialization",
    "load_specialization_from_string",
    "validate_handover",
]
