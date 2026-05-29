"""Serialize a Blueprint into a render-ready graph for the dashboards.

The dashboards used to hardcode their pipeline-flow node arrays (the ALM
dashboard's `PIPELINE_STAGES`, the SRE dashboard's `SRE_AGENTS`), which drifted
from the blueprint YAML that actually drives execution. This module makes the
blueprint the single source of truth: it flattens a `Blueprint` into a flat
`{nodes, edges, lanes}` graph the UI can draw with zero interpretation.

The server does the graph math once:

  * **nodes**          — one per `StageSpec` (`StageSpec.to_dict()` + a
                         `parallel_group`).
  * **edges**          — derived from each stage's `depends_on`. A stage that
                         carries a `condition:` produces `conditional` edges
                         (with the raw expression as `label`); otherwise
                         `sequence`.
  * **parallel_group** — stages that share a topo-sort level (no inter-dependency,
                         run concurrently) get the same group id so the renderer
                         can lay them out side by side.
  * **lanes**          — declared in `metadata.lanes`, or derived from the order
                         lanes first appear across nodes.

Lives in its own module (not on `Blueprint`) to avoid an import cycle:
`executor` imports `loader`, and we need `executor._topo_sort` here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from devai.blueprint.executor import _topo_sort

if TYPE_CHECKING:
    from devai.blueprint.loader import Blueprint


def build_blueprint_graph(bp: Blueprint) -> dict[str, Any]:
    """Flatten a Blueprint into a render-ready `{name,title,description,
    lanes,nodes,edges,levels}` dict.

    May raise ``BlueprintExecutionError`` if the blueprint has a dependency
    cycle (same check the executor applies before running it).
    """
    levels = _topo_sort(bp.stages)

    # parallel_group: stages sharing a level run concurrently → same group.
    # Single-stage levels get no group (None) so they render as plain nodes.
    group_of: dict[str, str | None] = {}
    for idx, level in enumerate(levels):
        group_id = f"level-{idx}" if len(level) > 1 else None
        for spec in level:
            group_of[spec.name] = group_id

    nodes: list[dict[str, Any]] = []
    for spec in bp.stages:
        node = spec.to_dict()
        node["parallel_group"] = group_of.get(spec.name)
        nodes.append(node)

    edges: list[dict[str, Any]] = []
    for spec in bp.stages:
        kind = "conditional" if spec.condition else "sequence"
        label = spec.condition or ""
        for dep in spec.depends_on:
            edges.append({"from": dep, "to": spec.name, "kind": kind, "label": label})

    meta = bp.metadata or {}
    lanes = meta.get("lanes")
    if not isinstance(lanes, list):
        lanes = []
    if not lanes:
        # Derive lane order from first appearance across nodes.
        seen: list[str] = []
        for node in nodes:
            lane = node.get("lane") or ""
            if lane and lane not in seen:
                seen.append(lane)
        lanes = seen

    return {
        "name": bp.name,
        "title": str(meta.get("title") or bp.name),
        "description": bp.description,
        "lanes": lanes,
        "nodes": nodes,
        "edges": edges,
        # Topo levels (lists of stage names) — handy for layout / debugging.
        "levels": [[spec.name for spec in level] for level in levels],
    }


__all__ = ["build_blueprint_graph"]
