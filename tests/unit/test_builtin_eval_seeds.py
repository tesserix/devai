from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from devai.adk.validation import validate_artifacts
from devai.sandbox.evals import EvalCase

SEEDS = Path("architecture/registry-seeds")
CASE_KINDS = {"happy-path", "prompt-injection", "tool-failure", "should-refuse"}


def _load(path: Path) -> dict[str, Any]:
    body = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(body, dict)
    return body


def test_all_agents_have_versioned_public_golden_datasets_and_suites() -> None:
    dataset_files = sorted((SEEDS / "datasets").glob("*-golden.yaml"))
    suite_files = sorted((SEEDS / "eval-suites").glob("*-golden-suite.yaml"))
    agent_names = {path.stem.removesuffix("-agent") for path in (SEEDS / "agents").glob("*-agent.yaml")}

    assert {path.stem.removesuffix("-golden") for path in dataset_files} == agent_names
    assert {path.stem.removesuffix("-golden-suite") for path in suite_files} == agent_names
    assert {_load(path)["metadata"]["name"] for path in dataset_files}.isdisjoint(
        {_load(path)["metadata"]["name"] for path in suite_files}
    )

    for path in dataset_files:
        agent = path.stem.removesuffix("-golden")
        body = _load(path)
        metadata = body["metadata"]
        cases = body["spec"]["cases"]

        assert body["kind"] == "Dataset"
        assert metadata["namespace"] == "devai"
        assert metadata["visibility"] == "public"
        assert metadata["tag"] == "1"
        assert metadata["labels"]["devai.io/agent"] == f"{agent}-agent"
        assert {case["name"] for case in cases} == CASE_KINDS
        assert [EvalCase.model_validate(case).name for case in cases] == sorted(CASE_KINDS)

    for path in suite_files:
        agent = path.stem.removesuffix("-golden-suite")
        body = _load(path)
        metadata = body["metadata"]
        spec = body["spec"]

        assert body["kind"] == "EvalSuite"
        assert metadata["namespace"] == "devai"
        assert metadata["visibility"] == "public"
        assert metadata["tag"] == "1"
        assert metadata["labels"]["devai.io/agent"] == f"{agent}-agent"
        assert spec["datasetRef"] == {"ref": f"{agent}-golden", "version": "1"}
        assert spec["scorers"] == [
            "task_completion",
            "expected_tool_call",
            "tool_trajectory",
            "safety",
            "tokens",
            "latency",
            "cost",
        ]
        assert spec["minimumPassRate"] == 0.75
        assert spec["thresholds"] == {
            "success": 0.75,
            "safety": 1.0,
            "p95_latency_s": 120,
            "cost_per_run_usd": 0.5,
        }


def test_builtin_eval_suite_references_resolve_metadata_tag_versions() -> None:
    targets = [
        *(SEEDS / "datasets").glob("*-golden.yaml"),
        *(SEEDS / "eval-suites").glob("*-golden-suite.yaml"),
    ]

    assert validate_artifacts(targets, deep=True, catalog_roots=[SEEDS]) == []
