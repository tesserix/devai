from pathlib import Path

import yaml

WORKFLOW = Path(__file__).parents[2] / ".github/workflows/agent-evals.yaml"


def _jobs() -> dict[str, object]:
    document = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    return document["jobs"]


def test_live_evaluations_require_complete_owner_scoped_configuration() -> None:
    jobs = _jobs()
    live_config = jobs["live_config"]
    evaluate = jobs["evaluate"]

    assert live_config["outputs"]["enabled"] == "${{ steps.detect.outputs.enabled }}"
    assert evaluate["needs"] == ["impact", "live_config"]
    assert "needs.live_config.outputs.enabled == 'true'" in evaluate["if"]


def test_scorecard_reports_missing_live_configuration_without_failing_static_gate() -> None:
    jobs = _jobs()
    scorecard = jobs["scorecard"]
    build_step = next(step for step in scorecard["steps"] if step["name"] == "Build consolidated scorecard")

    assert scorecard["needs"] == ["impact", "live_config", "evaluate"]
    assert "LIVE_EVALS_ENABLED" in build_step["env"]
    assert "Live evaluations skipped" in build_step["run"]
