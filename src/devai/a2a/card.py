"""Typed view over an A2A Agent Card.

The card is parsed leniently: every field is optional, and the full original
document is retained in ``raw`` so callers can read fields this dataclass
doesn't model yet (forward-compatibility with newer A2A versions).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class AgentSkill:
    """One capability the agent advertises."""

    id: str
    name: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)
    input_modes: list[str] = field(default_factory=list)
    output_modes: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AgentSkill:
        return cls(
            id=str(d.get("id") or d.get("name") or ""),
            name=str(d.get("name") or ""),
            description=str(d.get("description") or ""),
            tags=[str(t) for t in (d.get("tags") or [])],
            examples=[str(e) for e in (d.get("examples") or [])],
            input_modes=[str(m) for m in (d.get("inputModes") or [])],
            output_modes=[str(m) for m in (d.get("outputModes") or [])],
            raw=d,
        )


@dataclass(slots=True)
class AgentCard:
    """Parsed A2A Agent Card — the capability descriptor a consumer acts on."""

    name: str
    description: str = ""
    version: str = ""
    protocol_version: str = ""
    url: str = ""
    preferred_transport: str = "JSONRPC"
    capabilities: dict[str, Any] = field(default_factory=dict)
    default_input_modes: list[str] = field(default_factory=list)
    default_output_modes: list[str] = field(default_factory=list)
    skills: list[AgentSkill] = field(default_factory=list)
    provider: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AgentCard:
        if not isinstance(d, dict):
            raise ValueError("agent card must be a JSON object")
        return cls(
            name=str(d.get("name") or ""),
            description=str(d.get("description") or ""),
            version=str(d.get("version") or ""),
            protocol_version=str(d.get("protocolVersion") or ""),
            url=str(d.get("url") or ""),
            preferred_transport=str(d.get("preferredTransport") or "JSONRPC"),
            capabilities=dict(d.get("capabilities") or {}),
            default_input_modes=[str(m) for m in (d.get("defaultInputModes") or [])],
            default_output_modes=[str(m) for m in (d.get("defaultOutputModes") or [])],
            skills=[AgentSkill.from_dict(s) for s in (d.get("skills") or []) if isinstance(s, dict)],
            provider=dict(d.get("provider") or {}),
            raw=d,
        )

    # -- convenience accessors used by the orchestrator ------------------- #

    def has_skill(self, skill_id: str) -> bool:
        return any(s.id == skill_id for s in self.skills)

    def skill_ids(self) -> list[str]:
        return [s.id for s in self.skills]

    def matches(self, query: str) -> bool:
        """True if the query (case-insensitive) hits the agent name,
        description, or any skill id/name/tag — the capability-routing test."""
        q = query.lower().strip()
        if not q:
            return True
        if q in self.name.lower() or q in self.description.lower():
            return True
        for s in self.skills:
            if q in s.id.lower() or q in s.name.lower():
                return True
            if any(q in t.lower() for t in s.tags):
                return True
        return False

    @property
    def provenance(self) -> dict[str, Any]:
        """Registry provenance (arn/digest/ref/registry) carried in the card's
        capabilities.extensions, or {} when absent."""
        for ext in self.capabilities.get("extensions") or []:
            if isinstance(ext, dict) and ext.get("uri", "").endswith("/ext/provenance"):
                params = ext.get("params")
                return params if isinstance(params, dict) else {}
        return {}
