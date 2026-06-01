"""Crew data types — the in-memory shape a crew (DB row or seed YAML) parses into.

A `CrewSpec` references existing specializations by name; the runner resolves
each to a `Specialization` at execution time. `lead` must be one of the
members. `allowed_tools` on a member, when set, OVERRIDES the spec's own
allowed_tools for that crew (so the same `senior_developer` spec can be given
shell+checkpoint in one crew and a narrower set in another).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class CrewMember:
    specialization: str
    role_label: str = ""  # human label for the dashboard ("React Developer")
    allowed_tools: list[str] = field(default_factory=list)  # overrides spec.allowed_tools when non-empty

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CrewMember:
        return cls(
            specialization=str(data.get("specialization") or data.get("spec") or ""),
            role_label=str(data.get("role_label", "")),
            allowed_tools=list(data.get("allowed_tools") or []),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "specialization": self.specialization,
            "role_label": self.role_label,
            "allowed_tools": list(self.allowed_tools),
        }


@dataclass(slots=True)
class CrewSpec:
    name: str
    members: list[CrewMember]
    lead: str
    display_name: str = ""
    description: str = ""

    def __post_init__(self) -> None:
        if not self.members:
            raise ValueError(f"crew {self.name!r} has no members")
        spec_names = {m.specialization for m in self.members}
        if not self.lead:
            # default the lead to the first member
            self.lead = self.members[0].specialization
        if self.lead not in spec_names:
            raise ValueError(f"crew {self.name!r}: lead {self.lead!r} is not one of its members {sorted(spec_names)}")

    @property
    def non_lead_members(self) -> list[CrewMember]:
        return [m for m in self.members if m.specialization != self.lead]

    def member_for(self, specialization: str) -> CrewMember | None:
        return next((m for m in self.members if m.specialization == specialization), None)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CrewSpec:
        raw_members = data.get("members") or []
        members = [
            CrewMember.from_dict(m) if isinstance(m, dict) else CrewMember(specialization=str(m)) for m in raw_members
        ]
        return cls(
            name=str(data.get("name") or ""),
            members=members,
            lead=str(data.get("lead", "")),
            display_name=str(data.get("display_name", "")),
            description=str(data.get("description", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "lead": self.lead,
            "members": [m.to_dict() for m in self.members],
        }


__all__ = ["CrewMember", "CrewSpec"]
