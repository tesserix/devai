"""Tests for the render-ready blueprint graph (the data the dashboards draw).

Covers blueprint/graph.py::build_blueprint_graph and the StageSpec render
metadata + fallbacks in blueprint/loader.py. No Redis/Postgres/LLM — pure
in-memory parsing, so it runs in milliseconds.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from devai.blueprint.graph import build_blueprint_graph
from devai.blueprint.loader import (
    BlueprintLoadError,
    StageSpec,
    discover_blueprints,
    load_blueprint_from_string,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
BLUEPRINTS_DIR = REPO_ROOT / "blueprints"


# ── StageSpec render metadata + fallbacks ──────────────────────────────────


def test_stage_spec_title_humanizes_name_by_default() -> None:
    spec = StageSpec(name="implement-code", stage="implement_code")
    assert spec.display_title() == "Implement Code"


def test_stage_spec_explicit_title_and_render_fields() -> None:
    spec = StageSpec(
        name="review-code",
        stage="review_code",
        title="Code Review",
        lane="review",
        color="violet",
        gate=True,
    )
    d = spec.to_dict()
    assert d["title"] == "Code Review"
    assert d["lane"] == "review"
    assert d["color"] == "violet"
    assert d["gate"] is True


def test_resolved_agent_falls_back_to_config() -> None:
    spec = StageSpec(name="impl", stage="implement_code", config={"agent": "senior_developer"})
    assert spec.resolved_agent() == "senior_developer"
    # explicit agent: wins over config
    spec2 = StageSpec(name="impl", stage="implement_code", agent="staff_dev", config={"agent": "senior_developer"})
    assert spec2.resolved_agent() == "staff_dev"


# ── Graph builder ──────────────────────────────────────────────────────────


_BP = """
name: demo
description: tiny demo blueprint
metadata:
  title: Demo Flow
  lanes: [plan, build]
stages:
  - name: start
    stage: create_issue
    lane: plan
  - name: branch-a
    stage: noop
    lane: build
    depends_on: [start]
  - name: branch-b
    stage: noop
    lane: build
    depends_on: [start]
  - name: gated
    stage: noop
    lane: build
    depends_on: [branch-a, branch-b]
    condition: task.has_pr
    gate: true
"""


def test_graph_shape_nodes_edges_lanes() -> None:
    bp = load_blueprint_from_string(_BP)
    g = build_blueprint_graph(bp)

    assert g["name"] == "demo"
    assert g["title"] == "Demo Flow"  # from metadata.title
    assert g["lanes"] == ["plan", "build"]  # from metadata.lanes

    names = [n["name"] for n in g["nodes"]]
    assert names == ["start", "branch-a", "branch-b", "gated"]


def test_graph_parallel_group_shared_for_same_level() -> None:
    bp = load_blueprint_from_string(_BP)
    g = build_blueprint_graph(bp)
    by_name = {n["name"]: n for n in g["nodes"]}

    # branch-a and branch-b both depend only on `start` → same topo level → grouped.
    assert by_name["branch-a"]["parallel_group"] is not None
    assert by_name["branch-a"]["parallel_group"] == by_name["branch-b"]["parallel_group"]
    # `start` is alone on its level → no group.
    assert by_name["start"]["parallel_group"] is None


def test_graph_edges_sequence_vs_conditional() -> None:
    bp = load_blueprint_from_string(_BP)
    g = build_blueprint_graph(bp)

    seq = [(e["from"], e["to"]) for e in g["edges"] if e["kind"] == "sequence"]
    cond = [e for e in g["edges"] if e["kind"] == "conditional"]

    assert ("start", "branch-a") in seq
    assert ("start", "branch-b") in seq
    # `gated` has a condition → its incoming edges are conditional + labeled.
    cond_targets = {(e["from"], e["to"]) for e in cond}
    assert ("branch-a", "gated") in cond_targets
    assert ("branch-b", "gated") in cond_targets
    assert all(e["label"] == "task.has_pr" for e in cond)


def test_lanes_derived_when_metadata_absent() -> None:
    text = """
name: nolanes
description: ""
stages:
  - name: a
    stage: noop
    lane: build
  - name: b
    stage: noop
    lane: plan
    depends_on: [a]
"""
    bp = load_blueprint_from_string(text)
    g = build_blueprint_graph(bp)
    # No metadata.lanes → derived from first appearance order across nodes.
    assert g["lanes"] == ["build", "plan"]
    assert g["title"] == "nolanes"  # falls back to blueprint name


def test_unknown_render_key_rejected() -> None:
    text = """
name: bad
description: ""
stages:
  - name: a
    stage: noop
    bogus: 1
"""
    with pytest.raises(BlueprintLoadError):
        load_blueprint_from_string(text)


# ── Real shipped blueprints all serialize ──────────────────────────────────


def test_all_shipped_blueprints_build_a_graph() -> None:
    blueprints = discover_blueprints(BLUEPRINTS_DIR)
    assert blueprints, "expected shipped blueprints under blueprints/"
    for name, bp in blueprints.items():
        g = build_blueprint_graph(bp)
        assert g["name"] == name
        assert len(g["nodes"]) == len(bp.stages)
        # every edge references real nodes
        node_names = {n["name"] for n in g["nodes"]}
        for e in g["edges"]:
            assert e["from"] in node_names
            assert e["to"] in node_names
        # every node has a non-empty render title
        assert all(n["title"] for n in g["nodes"])


def test_alm_pipeline_groups_parallel_context_stages() -> None:
    blueprints = discover_blueprints(BLUEPRINTS_DIR)
    alm = blueprints["alm-pipeline"]
    g = build_blueprint_graph(alm)
    by_name = {n["name"]: n for n in g["nodes"]}
    # hydrate-context and inject-memory both depend only on create-issue.
    grp_h = by_name["hydrate-context"]["parallel_group"]
    grp_m = by_name["inject-memory"]["parallel_group"]
    assert grp_h is not None and grp_h == grp_m
