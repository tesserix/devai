"""CI-mode test execution — the QA agent's tests run through the repo's own
GitHub Actions when the local node toolchain is absent (the prod api pod is a
Python image: git yes, node/npm NO — locally executing Playwright there was
physically impossible and surfaced as misleading errors)."""

from __future__ import annotations

import pytest

from devai.tools.test_tools import TestToolExecutor


class _SCM:
    def __init__(self, runs, jobs=None):
        self.runs = runs
        self.jobs = jobs or []

    async def get_pipeline_runs(self, repo, branch=None, limit=5):
        return self.runs

    async def get_pipeline_jobs(self, repo, run_id):
        return self.jobs


@pytest.mark.asyncio
async def test_ci_mode_green_run():
    scm = _SCM(
        runs=[{"id": 1, "status": "completed", "conclusion": "success", "name": "CI", "html_url": "u"}],
        jobs=[{"name": "test", "conclusion": "success", "steps": []}],
    )
    result = await TestToolExecutor(scm)._run_via_ci("o/r", "feature/x")
    assert result["success"] is True
    assert result["mode"] == "ci"
    assert result["summary"]["passed"] == 1
    assert result["summary"]["failures"] == []


@pytest.mark.asyncio
async def test_ci_mode_failure_names_failed_steps():
    scm = _SCM(
        runs=[{"id": 2, "status": "completed", "conclusion": "failure", "name": "CI", "html_url": "u"}],
        jobs=[
            {"name": "build", "conclusion": "success", "steps": []},
            {
                "name": "unit-tests",
                "conclusion": "failure",
                "html_url": "j",
                "steps": [
                    {"name": "npm test", "conclusion": "failure"},
                    {"name": "checkout", "conclusion": "success"},
                ],
            },
        ],
    )
    result = await TestToolExecutor(scm)._run_via_ci("o/r", "feature/x")
    assert result["success"] is False
    assert result["summary"]["failed"] == 1
    assert result["summary"]["failures"][0]["test"] == "unit-tests"
    assert "npm test" in result["summary"]["failures"][0]["error"]


@pytest.mark.asyncio
async def test_ci_mode_no_runs_is_actionable():
    ex = TestToolExecutor(_SCM(runs=[]))
    ex._CI_MAX_WAIT_SECONDS = 0.0  # don't actually poll in tests
    result = await ex._run_via_ci("o/r", "feature/x")
    assert result["success"] is False
    assert "no CI workflow runs" in result["error"]


@pytest.mark.asyncio
async def test_ci_mode_without_scm_is_explicit():
    result = await TestToolExecutor(None)._run_via_ci("o/r", "feature/x")
    assert result["success"] is False
    assert "no node toolchain" in result["error"]
