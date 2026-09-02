from pathlib import Path

import yaml

WORKFLOW = Path(__file__).parents[2] / ".github/workflows/agent-evals.yaml"


def _workflow() -> dict[str, object]:
    return yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def _jobs() -> dict[str, object]:
    return _workflow()["jobs"]


def test_registry_dependencies_and_blueprints_trigger_agent_evaluations() -> None:
    triggers = _workflow()["on"]
    required_paths = {
        "architecture/registry-seeds/skills/**",
        "architecture/registry-seeds/tools/**",
        "architecture/registry-seeds/mcp-servers/**",
        "architecture/registry-seeds/datasets/**",
        "architecture/registry-seeds/eval-suites/**",
        "architecture/registry-seeds/blueprints/**",
        "architecture/registry-seeds/workflows/**",
        "blueprints/**",
    }

    for event in ("pull_request", "push"):
        assert required_paths <= set(triggers[event]["paths"])


def test_live_evaluations_require_complete_owner_scoped_configuration() -> None:
    jobs = _jobs()
    live_config = jobs["live_config"]
    evaluate = jobs["evaluate"]

    assert live_config["outputs"]["enabled"] == "${{ steps.detect.outputs.enabled }}"
    assert evaluate["needs"] == ["impact", "live_config", "publish_dependencies"]
    assert "needs.live_config.outputs.enabled == 'true'" in evaluate["if"]


def test_scorecard_fails_when_live_evaluation_configuration_is_missing() -> None:
    jobs = _jobs()
    scorecard = jobs["scorecard"]
    build_step = next(step for step in scorecard["steps"] if step["name"] == "Build consolidated scorecard")

    assert scorecard["needs"] == ["impact", "live_config", "evaluate"]
    assert "LIVE_EVALS_ENABLED" in build_step["env"]
    assert "Live evaluations skipped" in build_step["run"]
    assert "if [[ \"$LIVE_EVALS_ENABLED\" != 'true' ]]" in build_step["run"]


def test_push_publishes_dependencies_before_evaluation_then_releases_the_agent_and_workflows() -> None:
    jobs = _jobs()
    dependency_job = jobs["publish_dependencies"]
    evaluate = jobs["evaluate"]
    dependency_step = next(step for step in dependency_job["steps"] if step["name"] == "Publish changed dependencies")
    gate_step = next(step for step in evaluate["steps"] if step["name"] == "Evaluate and compare")
    release_step = next(step for step in evaluate["steps"] if step["name"] == "Publish evaluated Agent and workflows")

    assert evaluate["needs"] == ["impact", "live_config", "publish_dependencies"]
    assert dependency_step["if"] == "github.event_name == 'push'"
    assert release_step["if"] == "github.event_name == 'push'"
    assert "changed_dependency_paths" in dependency_step["run"]
    assert "candidate_run_id" in gate_step["run"]
    assert '--eval-run-id "$CANDIDATE_RUN_ID"' in release_step["run"]
    assert release_step["run"].index('devai adk publish "$AGENT_PATH"') < release_step["run"].index(
        'for artifact in "${release_paths[@]}"'
    )
