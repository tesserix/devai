from __future__ import annotations

from pathlib import Path

import yaml

from devai.ci.agent_evals import compare_scorecards, main, render_comparison_markdown, resolve_impact


def _write_yaml(root: Path, relative: str, document: dict[str, object]) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document), encoding="utf-8")


def _catalog(root: Path) -> None:
    _write_yaml(
        root,
        "architecture/registry-seeds/agents/reviewer-agent.yaml",
        {
            "kind": "Agent",
            "metadata": {"name": "reviewer-agent", "labels": {"devai.io/skill": "reviewer"}},
            "spec": {
                "promptRef": "reviewer-prompt-v1",
                "runtime": {"pythonClass": "devai.agents.reviewer.ReviewerAgent"},
            },
        },
    )

    _write_yaml(
        root,
        "architecture/registry-seeds/agents/planner-agent.yaml",
        {
            "kind": "Agent",
            "metadata": {"name": "planner-agent", "labels": {"devai.io/skill": "planner"}},
            "spec": {"promptRef": "planner-prompt-v1"},
        },
    )
    _write_yaml(
        root,
        "architecture/registry-seeds/eval-suites/reviewer-golden-suite.yaml",
        {
            "kind": "EvalSuite",
            "metadata": {
                "name": "reviewer-golden-suite",
                "tag": "1",
                "labels": {"devai.io/agent": "reviewer-agent"},
            },
            "spec": {"datasetRef": {"ref": "reviewer-golden", "version": "1"}},
        },
    )
    _write_yaml(
        root,
        "architecture/registry-seeds/datasets/reviewer-golden.yaml",
        {"kind": "Dataset", "metadata": {"name": "reviewer-golden"}, "spec": {"version": "1", "cases": []}},
    )


def test_resolve_impact_maps_agent_and_prompt_changes_to_the_owned_suite(tmp_path: Path) -> None:
    _catalog(tmp_path)

    impact = resolve_impact(
        tmp_path,
        [
            "architecture/registry-seeds/agents/reviewer-agent.yaml",
            "architecture/registry-seeds/prompts/reviewer-prompt-v1.yaml",
        ],
    )

    assert impact == {
        "evaluations": [
            {
                "agent": "reviewer-agent",
                "agent_path": "architecture/registry-seeds/agents/reviewer-agent.yaml",
                "dataset_path": "architecture/registry-seeds/datasets/reviewer-golden.yaml",
                "judge": False,
                "suite": "reviewer-golden-suite",
                "suite_path": "architecture/registry-seeds/eval-suites/reviewer-golden-suite.yaml",
            }
        ],
        "uncovered_agents": [],
    }


def test_resolve_impact_maps_specializations_and_python_sources(tmp_path: Path) -> None:
    _catalog(tmp_path)

    specific = resolve_impact(tmp_path, ["src/devai/agents/reviewer.py"])
    specialization = resolve_impact(tmp_path, ["specializations/review/reviewer.yaml"])
    shared = resolve_impact(tmp_path, ["src/devai/agents/skills/profiles.py"])

    assert [item["agent"] for item in specific["evaluations"]] == ["reviewer-agent"]
    assert [item["agent"] for item in specialization["evaluations"]] == ["reviewer-agent"]
    assert [item["agent"] for item in shared["evaluations"]] == ["reviewer-agent"]


def test_resolve_impact_reports_uncovered_agents_and_ignores_unrelated_changes(tmp_path: Path) -> None:
    _catalog(tmp_path)

    uncovered = resolve_impact(
        tmp_path,
        ["architecture/registry-seeds/agents/planner-agent.yaml"],
    )
    unrelated = resolve_impact(tmp_path, ["docs/readme.md"])

    assert uncovered == {"evaluations": [], "uncovered_agents": ["planner-agent"]}
    assert unrelated == {"evaluations": [], "uncovered_agents": []}


def test_compare_scorecards_reports_regressions_and_redacts_comment_content() -> None:
    baseline = {
        "summary": {"pass_rate": 1.0, "cost_usd": 0.1, "p95_latency_ms": 100},
        "results": [{"name": "refund", "passed": True, "failures": []}],
    }
    candidate = {
        "id": "eval-candidate",
        "summary": {"pass_rate": 0.0, "cost_usd": 0.12, "p95_latency_ms": 120},
        "results": [
            {
                "name": "refund",
                "passed": False,
                "failures": ["provider exposed sk-ant-super-secret-value"],
                "trace_id": "trace-refund",
            }
        ],
    }

    comparison = compare_scorecards("reviewer-agent", baseline, candidate)
    markdown = render_comparison_markdown(comparison)

    assert comparison["status"] == "regressed"
    assert comparison["metrics"]["pass_rate"]["delta"] == -1.0
    assert comparison["regressions"] == ["refund"]
    assert "eval-candidate" in markdown
    assert "refund" in markdown
    assert "sk-ant-***" in markdown
    assert "super-secret-value" not in markdown


def test_compare_command_writes_machine_and_human_scorecards(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.json"
    candidate = tmp_path / "candidate.json"
    output = tmp_path / "comparison.json"
    markdown = tmp_path / "comparison.md"
    baseline.write_text('{"summary":{"pass_rate":1},"results":[{"name":"case","passed":true}]}')
    candidate.write_text('{"id":"eval-2","summary":{"pass_rate":0},"results":[{"name":"case","passed":false}]}')

    exit_code = main(
        [
            "compare",
            "--agent",
            "reviewer-agent",
            "--baseline",
            str(baseline),
            "--candidate",
            str(candidate),
            "--output",
            str(output),
            "--markdown",
            str(markdown),
        ]
    )

    assert exit_code == 2
    assert yaml.safe_load(output.read_text())["status"] == "regressed"
    assert "reviewer-agent" in markdown.read_text()
