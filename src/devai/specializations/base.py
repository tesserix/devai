"""Specialization data types — the in-memory shape every YAML parses into.

Three companion enums + two dataclasses:

    LLMProvider        — claude | openai | groq | gemini | nemoclaw | codex | auto
    RiskLevel          — low | medium | high | critical (gates human review)
    HandoverField      — typed slot in a spec's handover_schema
    Specialization     — the spec itself
    Specialization.legacy_python_class — backward bridge to existing agent classes

The runner contract is intentionally Python-class-agnostic: a Specialization
can declare a Python class that implements the role (for the existing 13
ALM agents and 7 SRE agents) OR be YAML-only (for new Fiber-parity roles
like prototyper, intake, negotiator). When the runner ships in a later
slice, YAML-only specs will execute directly against the LLM provider.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class LLMProvider(str, Enum):
    """The LLM backends DevAI knows how to dispatch to.

    Mirrors the modules under `src/devai/providers/`. `AUTO` lets the
    runtime pick based on a per-role heuristic (typically the agent's
    hardcoded default).
    """

    CLAUDE = "claude"
    OPENAI = "openai"
    CODEX = "codex"  # OpenAI Codex sandbox
    GROQ = "groq"
    GEMINI = "gemini"
    NEMOCLAW = "nemoclaw"  # self-hosted Nemotron
    AUTO = "auto"

    @classmethod
    def parse(cls, value: Any, *, default: LLMProvider | None = None) -> LLMProvider:
        if value is None or value == "":
            return default if default is not None else cls.AUTO
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).lower())
        except ValueError:
            return default if default is not None else cls.AUTO


class RiskLevel(str, Enum):
    """How autonomously a role may act. Higher risk → mandatory human gate.

    The blueprint executor reads this from the resolved Specialization
    and, when the role's risk is high or critical, parks the task in
    `awaiting_approval` after the stage produces output.
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @classmethod
    def parse(cls, value: Any, *, default: RiskLevel | None = None) -> RiskLevel:
        if value is None or value == "":
            return default if default is not None else cls.MEDIUM
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).lower())
        except ValueError:
            return default if default is not None else cls.MEDIUM

    @property
    def needs_human_gate(self) -> bool:
        return self in {RiskLevel.HIGH, RiskLevel.CRITICAL}


@dataclass(slots=True, frozen=True)
class HandoverField:
    """One typed slot in a spec's `handover_schema`.

    The names mirror JSON Schema's vocabulary because that's what every
    LLM tool-calling layer already understands. We don't render full
    JSON Schema — the runner does its own check.
    """

    name: str
    type: str  # string | integer | number | boolean | array | object | any
    required: bool = True
    description: str = ""

    _VALID_TYPES = frozenset({"string", "integer", "number", "boolean", "array", "object", "any"})

    def __post_init__(self) -> None:
        if self.type not in self._VALID_TYPES:
            # Use object.__setattr__ since frozen dataclass — but raising
            # is the right call: bad spec, surface immediately.
            raise ValueError(
                f"handover field {self.name!r}: type must be one of {sorted(self._VALID_TYPES)}, got {self.type!r}"
            )

    def matches(self, value: Any) -> bool:
        """Light runtime check used by validate_handover."""
        if self.type == "any":
            return True
        if self.type == "string":
            return isinstance(value, str)
        if self.type == "integer":
            return isinstance(value, bool) is False and isinstance(value, int)
        if self.type == "number":
            return isinstance(value, int | float) and not isinstance(value, bool)
        if self.type == "boolean":
            return isinstance(value, bool)
        if self.type == "array":
            return isinstance(value, list)
        if self.type == "object":
            return isinstance(value, dict)
        return False


_VALID_ROLE_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(slots=True)
class Specialization:
    """One YAML spec, parsed.

    All fields populated by the loader. Mutating instances at runtime is
    discouraged — treat them as effectively immutable after `load_*`.
    """

    # ── Identity ────────────────────────────────────────────────────
    name: str
    display_name: str = ""
    description: str = ""
    category: str = "specialist"  # planning | coding | review | orchestration | sre | specialist

    # ── LLM dispatch ────────────────────────────────────────────────
    llm_provider: LLMProvider = LLMProvider.AUTO
    llm_model: str = ""  # empty → provider default
    temperature: float | None = None
    max_tokens: int | None = None

    # ── Role contract ───────────────────────────────────────────────
    system_prompt: str = ""
    user_prompt_template: str = ""  # optional jinja-style; loader doesn't render

    allowed_tools: list[str] = field(default_factory=list)  # tool names — gated by runner
    max_turns: int = 50
    timeout_seconds: int = 900

    # Which keys the spec expects to read out of task.agent_context before
    # it starts. Used by the runner to surface a clear error if a prior
    # stage failed to write its handover.
    context_keys: list[str] = field(default_factory=list)

    # The key under which this spec's output is written into agent_context.
    # Defaults to `<name>_output` (mirrors how AgentAdapter currently writes).
    output_key: str = ""

    handover_schema: dict[str, HandoverField] = field(default_factory=dict)

    # ── Governance ──────────────────────────────────────────────────
    risk_level: RiskLevel = RiskLevel.MEDIUM
    role_color: str = "engineer"  # engineer | ux | security | qa | product | admin

    # ── Backward bridge ─────────────────────────────────────────────
    # When set, the runner delegates execution to this Python class
    # instead of running the spec independently. This is how we ship
    # specializations *now* without rewriting the 20 existing agents.
    # Empty → YAML-only spec (runner uses the LLM directly).
    legacy_python_class: str = ""

    # Free-form metadata (versioning, owner, links, …). Surfaced to the
    # dashboard but never inspected by the runtime.
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not _VALID_ROLE_NAME.match(self.name):
            raise ValueError(
                f"specialization name {self.name!r} must be lowercase snake_case (regex: {_VALID_ROLE_NAME.pattern})"
            )
        if not self.output_key:
            object.__setattr__(self, "output_key", f"{self.name}_output")

    def required_handover_fields(self) -> list[HandoverField]:
        return [f for f in self.handover_schema.values() if f.required]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "category": self.category,
            "llm_provider": self.llm_provider.value,
            "llm_model": self.llm_model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "system_prompt_chars": len(self.system_prompt),
            "user_prompt_template_chars": len(self.user_prompt_template),
            "allowed_tools": list(self.allowed_tools),
            "max_turns": self.max_turns,
            "timeout_seconds": self.timeout_seconds,
            "context_keys": list(self.context_keys),
            "output_key": self.output_key,
            "handover_schema": {
                k: {"type": v.type, "required": v.required, "description": v.description}
                for k, v in self.handover_schema.items()
            },
            "risk_level": self.risk_level.value,
            "role_color": self.role_color,
            "legacy_python_class": self.legacy_python_class,
            "metadata": dict(self.metadata),
        }


__all__ = ["HandoverField", "LLMProvider", "RiskLevel", "Specialization"]
