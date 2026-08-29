"""The post_report run verdict — a run that built nothing must say so.

Every stage in a misconfigured run (no LLM, no SCM, no crew) degrades to a
stub and returns a *successful* StageResult, so the pipeline used to finish
DONE with an empty report. `assess_work` is the check that catches it, and
post_report turns it into an `ok=False` handover the executor honors.
"""

from __future__ import annotations

import pytest

from devai.pipeline.interfaces import StageDeps
from devai.pipeline.stages.lifecycle import assess_work, post_report_stage
from devai.pipeline.types import DevAITask


def test_stub_only_run_produced_no_work():
    task = DevAITask(intent="x", repo="tesserix/test-repo")
    task.agent_context = {
        "senior_developer_stub": True,
        "review_code_error": "no llm",
        "crew_output": {"crew_runner_stub": True},
    }
    produced, reason = assess_work(task)
    assert produced is False
    assert "stub" in reason


def test_run_with_a_pr_produced_work():
    task = DevAITask(intent="x")
    task.pr_number = 12
    assert assess_work(task)[0] is True


def test_run_with_agent_output_produced_work():
    task = DevAITask(intent="x")
    task.agent_context = {"security_expert_output": {"findings": []}, "review_notes": "looks fine"}
    assert assess_work(task)[0] is True


@pytest.mark.asyncio
async def test_post_report_flags_a_no_work_run():
    deps = StageDeps(config=None, llm=None, scm=None)
    task = DevAITask(intent="add a health endpoint", repo="tesserix/test-repo")
    task.agent_context = {"run_crew_stub": True}
    task.stages_completed = ["create-issue", "run-crew"]

    result = await post_report_stage(deps, {"target": "none"}).execute(task)

    assert result.data["ok"] is False
    assert result.data["run_verdict"] == "no_work"
    assert "produced no work" in result.data["report_markdown"]


@pytest.mark.asyncio
async def test_post_report_stays_green_when_work_happened():
    deps = StageDeps(config=None, llm=None, scm=None)
    task = DevAITask(intent="add a health endpoint", repo="tesserix/test-repo")
    task.agent_context = {
        "crew_output": {"crew": "backend_crew", "member_outputs": {"senior_developer": {"summary": "added"}}}
    }

    result = await post_report_stage(deps, {"target": "none"}).execute(task)

    assert result.data.get("ok") is not False
    assert "run_verdict" not in result.data
