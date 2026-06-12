"""CI ground-truth gate — red workflows can never pass a stage (live incident)."""

from __future__ import annotations

import sys
import types

import pytest

if "ulid" not in sys.modules:  # slim-env stub (same as test_task_liveness)
    _ulid = types.ModuleType("ulid")
    _ulid.ULID = type("ULID", (), {"__str__": lambda self: "01TEST"})
    sys.modules["ulid"] = _ulid

from devai.pipeline.interfaces import StageDeps
from devai.pipeline.stages.alm import _assert_ci_truth


class _Resp:
    def __init__(self, payload, status=200):
        self._p = payload
        self.status_code = status

    def json(self):
        return self._p


class _SCM:
    def __init__(self, runs=None, workflows=True):
        self._runs = runs or []
        self._workflows = workflows

    async def _request(self, method, path, params=None):
        if "actions/runs" in path:
            return _Resp({"workflow_runs": self._runs})
        if ".github/workflows" in path:
            return _Resp([{"name": "ci.yml"}] if self._workflows else [], 200 if self._workflows else 404)
        return _Resp({}, 404)


class _Task:
    repo = "tesserix/test-repo"
    branch_name = "story/60-x"


def _deps(scm) -> StageDeps:
    return StageDeps(config=object(), scm=scm)


def _run(wf, conclusion, status="completed"):
    return {"workflow_id": wf, "status": status, "conclusion": conclusion, "html_url": f"https://gh/{wf}"}


@pytest.mark.asyncio
async def test_red_workflows_fail_the_stage():
    deps = _deps(_SCM(runs=[_run(1, "failure"), _run(2, "success")]))
    with pytest.raises(RuntimeError, match="concluded 'failure'"):
        await _assert_ci_truth(deps, _Task(), {}, stage="monitor_build")


@pytest.mark.asyncio
async def test_every_workflows_newest_run_must_be_green():
    # Newest run of workflow 1 is green, but workflow 2's newest is red.
    deps = _deps(_SCM(runs=[_run(1, "success"), _run(2, "failure"), _run(2, "success")]))
    with pytest.raises(RuntimeError, match="failure"):
        await _assert_ci_truth(deps, _Task(), {}, stage="run_tests")


@pytest.mark.asyncio
async def test_all_green_passes():
    deps = _deps(_SCM(runs=[_run(1, "success"), _run(2, "success")]))
    await _assert_ci_truth(deps, _Task(), {}, stage="monitor_build")  # no raise


@pytest.mark.asyncio
async def test_in_progress_cannot_be_declared_success():
    deps = _deps(_SCM(runs=[_run(1, None, status="in_progress")]))
    with pytest.raises(RuntimeError, match="still running"):
        await _assert_ci_truth(deps, _Task(), {}, stage="monitor_build")


@pytest.mark.asyncio
async def test_no_runs_with_workflows_present_is_a_failure():
    # The vacuous-success hole: workflows exist but never triggered.
    deps = _deps(_SCM(runs=[], workflows=True))
    with pytest.raises(RuntimeError, match="never triggered"):
        await _assert_ci_truth(deps, _Task(), {}, stage="monitor_build")


@pytest.mark.asyncio
async def test_repo_without_ci_is_skipped_not_blocked():
    deps = _deps(_SCM(runs=[], workflows=False))
    await _assert_ci_truth(deps, _Task(), {}, stage="monitor_build")  # no raise


@pytest.mark.asyncio
async def test_non_github_scm_keeps_agent_verdict():
    class _NoRequest:
        pass

    await _assert_ci_truth(_deps(_NoRequest()), _Task(), {}, stage="monitor_build")  # no raise
