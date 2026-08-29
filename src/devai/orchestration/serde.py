"""(De)serialization between pipeline dataclasses and plain JSON-able dicts.

Temporal payloads must be plain data. These helpers convert ``Blueprint`` /
``StageSpec`` / ``DevAITask`` / ``StageResult`` to and from dicts so the workflow
and activity exchange them across the worker boundary. Kept pure (no I/O) so it is
safe to import inside the deterministic workflow sandbox.
"""

from __future__ import annotations

from typing import Any

from devai.blueprint.loader import Blueprint, StageSpec
from devai.pipeline.types import DevAITask, StageResult, TaskState

# --- StageSpec / Blueprint ------------------------------------------------


def stage_to_dict(spec: StageSpec) -> dict[str, Any]:
    return {
        "name": spec.name,
        "stage": spec.stage,
        "depends_on": list(spec.depends_on),
        "timeout_seconds": spec.timeout_seconds,
        "on_failure": spec.on_failure,
        "condition": spec.condition,
        "config": dict(spec.config),
        "type": spec.type,
        "title": spec.title,
        "lane": spec.lane,
        "color": spec.color,
        "agent": spec.agent,
        "gate": spec.gate,
        "parallel": spec.parallel,
    }


def stage_from_dict(d: dict[str, Any]) -> StageSpec:
    return StageSpec(
        name=d["name"],
        stage=d.get("stage", d["name"]),
        depends_on=list(d.get("depends_on", [])),
        timeout_seconds=d.get("timeout_seconds"),
        on_failure=d.get("on_failure", "stop"),
        condition=d.get("condition", ""),
        config=dict(d.get("config", {})),
        type=d.get("type", ""),
        title=d.get("title", ""),
        lane=d.get("lane", ""),
        color=d.get("color", ""),
        agent=d.get("agent", ""),
        gate=bool(d.get("gate", False)),
        parallel=bool(d.get("parallel", False)),
    )


def blueprint_to_dict(bp: Blueprint) -> dict[str, Any]:
    return {
        "name": bp.name,
        "description": bp.description,
        "stages": [stage_to_dict(s) for s in bp.stages],
        "metadata": dict(bp.metadata),
    }


def blueprint_from_dict(d: dict[str, Any]) -> Blueprint:
    return Blueprint(
        name=d["name"],
        description=d.get("description", ""),
        stages=[stage_from_dict(s) for s in d.get("stages", [])],
        metadata=dict(d.get("metadata", {})),
    )


# --- DevAITask ------------------------------------------------------------


def task_to_dict(task: DevAITask) -> dict[str, Any]:
    """Full task payload. Reuses DevAITask.to_dict() so no identity field
    (issue/crew/team/principal/trace, etc.) is silently dropped across the
    worker boundary — that is what lets *any* stage run durably."""
    return task.to_dict()


def task_from_dict(d: dict[str, Any]) -> DevAITask:
    """Exact inverse of :func:`task_to_dict`.

    Delegates to ``DevAITask.from_dict`` rather than re-listing fields. A
    hand-rolled inverse drifted: it omitted ``stage_events`` and ``agents``,
    so every Temporal round-trip wiped the run timeline and the dashboard's
    agent cards while ``stages_completed`` survived — a finished run rendered
    as "no agents yet".
    """
    return DevAITask.from_dict(d)


# --- StageResult ----------------------------------------------------------


def stage_result_to_dict(result: StageResult) -> dict[str, Any]:
    return {
        "next_state": result.next_state.value if result.next_state else None,
        "message": result.message,
        "data": dict(result.data),
    }


def stage_result_from_dict(d: dict[str, Any]) -> StageResult:
    ns = d.get("next_state")
    return StageResult(
        next_state=TaskState(ns) if ns else None,
        message=d.get("message", ""),
        data=dict(d.get("data", {})),
    )
