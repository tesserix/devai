"""Canonical, hash-verified snapshots of Registry-resolved Agents."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import asdict, fields, is_dataclass
from typing import Any

from devai.registry.client import Agent, ResolvedAgent, UnresolvedRef

_SCHEMA_VERSION = 1
_MAX_CANONICAL_BYTES = 512 * 1024
_AGENT_FIELDS = frozenset(field.name for field in fields(Agent))


class CompositionSnapshotError(ValueError):
    """A resolved composition snapshot is malformed or has drifted."""


def _canonical(payload: dict[str, Any]) -> bytes:
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    except (TypeError, ValueError) as error:
        raise CompositionSnapshotError("composition is not canonical JSON") from error
    if len(encoded) > _MAX_CANONICAL_BYTES:
        raise CompositionSnapshotError("composition snapshot exceeds 512 KiB")
    return encoded


def _digest(payload: dict[str, Any]) -> str:
    return f"sha256:{hashlib.sha256(_canonical(payload)).hexdigest()}"


def _record_dict(value: Any) -> dict[str, Any]:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, dict):
        return dict(value)
    attributes = getattr(value, "__dict__", None)
    if isinstance(attributes, dict):
        return dict(attributes)
    raise CompositionSnapshotError("composition record is invalid")


def snapshot_composition(resolved: ResolvedAgent) -> dict[str, Any]:
    """Serialize the exact Agent resolution and attach its canonical digest."""
    payload: dict[str, Any] = {
        "schema_version": _SCHEMA_VERSION,
        "agent": _record_dict(resolved.agent),
        "resolved": resolved.resolved,
        "unresolved": [_record_dict(reference) for reference in resolved.unresolved],
    }
    return {**payload, "digest": _digest(payload)}


def load_composition_snapshot(snapshot: dict[str, Any]) -> ResolvedAgent:
    """Verify and reconstruct a resolved Agent snapshot without network I/O."""
    if not isinstance(snapshot, dict):
        raise CompositionSnapshotError("composition snapshot must be an object")
    if set(snapshot) != {"schema_version", "agent", "resolved", "unresolved", "digest"}:
        raise CompositionSnapshotError("composition snapshot fields are invalid")
    if snapshot.get("schema_version") != _SCHEMA_VERSION:
        raise CompositionSnapshotError("composition snapshot schema is unsupported")

    supplied_digest = snapshot.get("digest")
    if not isinstance(supplied_digest, str):
        raise CompositionSnapshotError("composition snapshot digest is missing")
    payload = {key: value for key, value in snapshot.items() if key != "digest"}
    expected_digest = _digest(payload)
    if not hmac.compare_digest(supplied_digest, expected_digest):
        raise CompositionSnapshotError("composition snapshot digest mismatch")

    raw_agent = snapshot.get("agent")
    if not isinstance(raw_agent, dict) or set(raw_agent) != _AGENT_FIELDS:
        raise CompositionSnapshotError("composition Agent is invalid")
    try:
        agent = Agent(**raw_agent)
    except (TypeError, ValueError) as error:
        raise CompositionSnapshotError("composition Agent is invalid") from error
    if not agent.name or not agent.version:
        raise CompositionSnapshotError("composition Agent identity is incomplete")

    raw_resolved = snapshot.get("resolved")
    if not isinstance(raw_resolved, dict):
        raise CompositionSnapshotError("composition dependencies are invalid")
    resolved: dict[str, list[dict[str, Any]]] = {}
    for kind, entries in raw_resolved.items():
        if not isinstance(kind, str) or not isinstance(entries, list):
            raise CompositionSnapshotError("composition dependencies are invalid")
        if any(not isinstance(entry, dict) for entry in entries):
            raise CompositionSnapshotError("composition dependency is invalid")
        resolved[kind] = [dict(entry) for entry in entries]

    raw_unresolved = snapshot.get("unresolved")
    if not isinstance(raw_unresolved, list):
        raise CompositionSnapshotError("composition unresolved references are invalid")
    unresolved: list[UnresolvedRef] = []
    for reference in raw_unresolved:
        if not isinstance(reference, dict) or set(reference) != {"kind", "ref", "reason"}:
            raise CompositionSnapshotError("composition unresolved reference is invalid")
        if any(not isinstance(reference[key], str) for key in ("kind", "ref", "reason")):
            raise CompositionSnapshotError("composition unresolved reference is invalid")
        unresolved.append(UnresolvedRef(**reference))
    return ResolvedAgent(agent=agent, resolved=resolved, unresolved=unresolved)


__all__ = [
    "CompositionSnapshotError",
    "load_composition_snapshot",
    "snapshot_composition",
]
