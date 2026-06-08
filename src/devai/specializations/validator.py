"""Handover schema validation.

After a role produces output the stage runner calls `validate_handover`
to confirm every required field in the spec's `handover_schema` is
present and (loosely) the right type. This is what makes the multi-role
chain robust: a missing field is caught at the role boundary, surfaced
as a clear error, and the downstream role never sees malformed data.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from devai.specializations.base import Specialization


class HandoverValidationError(Exception):
    """Raised by strict callers; carries the structured violation list."""

    def __init__(self, spec_name: str, violations: list[HandoverViolation]) -> None:
        self.spec_name = spec_name
        self.violations = violations
        summary = "; ".join(str(v) for v in violations)
        super().__init__(f"handover validation failed for {spec_name}: {summary}")


@dataclass(slots=True, frozen=True)
class HandoverViolation:
    """One way the handover dict failed to match the spec."""

    field: str
    reason: str  # missing | wrong_type | empty
    expected: str = ""
    got: str = ""

    def __str__(self) -> str:
        if self.reason == "missing":
            return f"{self.field}: missing (expected {self.expected})"
        if self.reason == "wrong_type":
            return f"{self.field}: wrong type — expected {self.expected}, got {self.got}"
        if self.reason == "empty":
            return f"{self.field}: empty (expected non-empty {self.expected})"
        return f"{self.field}: {self.reason}"


def validate_handover(
    spec: Specialization,
    data: Any,
    *,
    strict: bool = False,
    require_non_empty: bool = False,
) -> list[HandoverViolation]:
    """Check `data` against `spec.handover_schema`.

    Args:
        spec: the Specialization whose handover_schema we're checking against
        data: whatever the role produced (typically a dict)
        strict: when True, raises HandoverValidationError if any required
                violation is found; otherwise returns the list.
        require_non_empty: when True, an empty string / empty list / empty
                dict for a required field counts as a violation too.

    Returns:
        list of HandoverViolation (empty list means clean handover).

    Optional fields are checked when present — wrong type still flags
    a violation even on optional fields, because returning the wrong
    shape downstream is just as bad as missing it.
    """
    violations: list[HandoverViolation] = []

    if not isinstance(data, dict):
        # If the spec declares ANY handover fields, the data should be a dict.
        if spec.handover_schema:
            violations.append(
                HandoverViolation(
                    field="<root>",
                    reason="wrong_type",
                    expected="object",
                    got=type(data).__name__,
                )
            )
        if strict and violations:
            raise HandoverValidationError(spec.name, violations)
        return violations

    for field_name, field_spec in spec.handover_schema.items():
        present = field_name in data
        if not present:
            if field_spec.required:
                violations.append(
                    HandoverViolation(
                        field=field_name,
                        reason="missing",
                        expected=field_spec.type,
                    )
                )
            continue

        value = data[field_name]

        # Allow null for optional fields; null for required is "missing".
        if value is None:
            if field_spec.required:
                violations.append(
                    HandoverViolation(
                        field=field_name,
                        reason="missing",
                        expected=field_spec.type,
                    )
                )
            continue

        if not field_spec.matches(value):
            violations.append(
                HandoverViolation(
                    field=field_name,
                    reason="wrong_type",
                    expected=field_spec.type,
                    got=type(value).__name__,
                )
            )
            continue

        if require_non_empty and field_spec.required and _is_empty(value):
            violations.append(HandoverViolation(field=field_name, reason="empty", expected=field_spec.type))

    if strict and violations:
        raise HandoverValidationError(spec.name, violations)
    return violations


def _is_empty(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip() == ""
    if isinstance(value, list | dict | tuple | set):
        return len(value) == 0
    return False


__all__ = ["HandoverValidationError", "HandoverViolation", "validate_handover"]
