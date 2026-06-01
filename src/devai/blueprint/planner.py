"""Pure blueprint planning primitives — DAG ordering + failure policy.

This module is deliberately **side-effect free and import-light**: no I/O, no
clocks, no randomness, no asyncio. That is what lets the very same ordering logic
run in two places without divergence:

  * the in-process :class:`~devai.blueprint.executor.BlueprintExecutor`, and
  * the deterministic Temporal ``BlueprintWorkflow`` (which replays history and
    therefore forbids any non-determinism).

Condition evaluation already has a canonical home in
:mod:`devai.blueprint.conditions` (a richer DSL); both backends use that, so it is
intentionally *not* duplicated here.

Keeping ordering here means a blueprint — simple or complex — is sequenced
identically no matter which orchestration backend runs it. Add a stage to a YAML
blueprint and it flows through both paths with zero code changes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from devai.blueprint.loader import StageSpec


def topological_levels(stages: list[StageSpec]) -> list[list[StageSpec]]:
    """Group *stages* into dependency levels (Kahn's algorithm).

    Each returned level is a list of stages whose dependencies are all satisfied
    by previous levels; stages within a level can run concurrently. Raises
    :class:`ValueError` on a dependency cycle.

    Ordering is stable: stages keep their blueprint-declared order within a level
    so that replay (Temporal) and direct execution (in-proc) agree exactly.
    """
    order = {s.name: i for i, s in enumerate(stages)}
    by_name = {s.name: s for s in stages}
    indegree = {s.name: 0 for s in stages}
    dependents: dict[str, list[str]] = {s.name: [] for s in stages}

    for s in stages:
        for dep in s.depends_on:
            if dep not in by_name:
                continue
            indegree[s.name] += 1
            dependents[dep].append(s.name)

    ready = sorted((name for name, d in indegree.items() if d == 0), key=order.__getitem__)
    levels: list[list[StageSpec]] = []
    seen = 0
    while ready:
        levels.append([by_name[n] for n in ready])
        seen += len(ready)
        nxt: list[str] = []
        for name in ready:
            for dep in dependents[name]:
                indegree[dep] -= 1
                if indegree[dep] == 0:
                    nxt.append(dep)
        ready = sorted(nxt, key=order.__getitem__)

    if seen != len(stages):
        raise ValueError("blueprint has a dependency cycle")

    return levels


def should_continue_on_failure(on_failure: str) -> bool:
    """Return True if a stage failure should NOT halt the blueprint.

    Mirrors :class:`BlueprintExecutor` semantics: ``continue`` keeps going;
    ``stop`` / ``rollback`` halt.
    """
    return (on_failure or "stop").strip().lower() == "continue"
