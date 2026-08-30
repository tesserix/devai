"""Dispatcher-side envelope hand-off for eval-gated agents.

The sandbox boundary rescopes secrets away from eval Jobs, so the runner
cannot query the registry for private (user-published) records. The dispatcher
resolves them with its authenticated client and must ship the gate-stamped
envelope inside the agent profile it already bakes into the Job env.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from devai.evaluations.gates import EVAL_GATE_LABEL
from devai.pipeline.interfaces import StageDeps
from devai.pipeline.stages.job_runner import JobRunnerStage


def _agent_meta(labels: dict[str, str], raw: dict):
    return SimpleNamespace(
        name="measure-mate-agent",
        image="",
        description="",
        version="1",
        framework="",
        language="",
        model_provider="vertex_gemini",
        model_name="gemini-2.5-flash",
        skills=["unit-conversion"],
        prompts=[],
        mcp_servers=[],
        labels=labels,
        raw=raw,
    )


def _stage(agent_meta) -> JobRunnerStage:
    config = SimpleNamespace(agentgateway_url="", kagent_url="")
    registry = SimpleNamespace(get_agent=lambda n: agent_meta)
    deps = StageDeps(config=config, extra={"registry_client": registry})
    return JobRunnerStage(deps, {"__stage_name": "evaluation"})


@pytest.mark.asyncio
async def test_eval_gated_profile_carries_the_registry_envelope() -> None:
    raw = {"name": "measure-mate-agent", "labels": {EVAL_GATE_LABEL: "passed"}, "systemPrompt": "convert"}
    profile = await _stage(_agent_meta({EVAL_GATE_LABEL: "passed"}, raw))._fetch_agent_profile("measure-mate-agent")

    assert profile is not None
    assert profile["envelope"] == raw


@pytest.mark.asyncio
async def test_ungated_profile_stays_lean() -> None:
    raw = {"name": "measure-mate-agent", "labels": {}}
    profile = await _stage(_agent_meta({}, raw))._fetch_agent_profile("measure-mate-agent")

    assert profile is not None
    assert "envelope" not in profile
