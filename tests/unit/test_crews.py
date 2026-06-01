"""Unit tests for AI agent crews (Phase 3).

Covers the crew models, the seed-YAML loader (against the real crews/ dir),
and the CrewRunnerStage end-to-end with a scripted FakeLLM driving the lead's
plan and each member's output — no cluster, no real model.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from devai.adapters.llm.base import LLMResponse, LLMUsage
from devai.crews.loader import load_seed_crews
from devai.crews.models import CrewMember, CrewSpec
from devai.pipeline.interfaces import StageDeps
from devai.pipeline.stages.crew_runner import crew_runner_stage
from devai.pipeline.types import DevAITask
from devai.specializations.base import Specialization

REPO_ROOT = Path(__file__).resolve().parents[2]


# ── models ──────────────────────────────────────────────────────────────


def test_crew_lead_defaults_to_first_member():
    crew = CrewSpec(name="c", members=[CrewMember("a"), CrewMember("b")], lead="")
    assert crew.lead == "a"
    assert [m.specialization for m in crew.non_lead_members] == ["b"]


def test_crew_rejects_lead_not_in_members():
    with pytest.raises(ValueError, match="lead"):
        CrewSpec(name="c", members=[CrewMember("a")], lead="zzz")


def test_crew_from_dict_mixed_member_shapes():
    crew = CrewSpec.from_dict(
        {"name": "c", "lead": "a", "members": [{"specialization": "a", "allowed_tools": ["shell_exec"]}, "b"]}
    )
    assert crew.member_for("a").allowed_tools == ["shell_exec"]
    assert crew.member_for("b").specialization == "b"


# ── seed loader ─────────────────────────────────────────────────────────


def test_seed_crews_load_from_repo():
    crews = load_seed_crews(REPO_ROOT / "crews")
    assert "frontend_crew" in crews
    assert "backend_crew" in crews
    fe = crews["frontend_crew"]
    assert fe.lead == "senior_developer"
    # the developer member is granted the Cursor capability tools
    dev = fe.member_for("senior_developer")
    assert "shell_exec" in dev.allowed_tools and "checkpoint" in dev.allowed_tools


# ── CrewRunnerStage ─────────────────────────────────────────────────────


class FakeLLMAdapter:
    def __init__(self, responses):
        self._responses = list(responses)

    async def generate(self, request):  # noqa: ANN001
        if not self._responses:
            return LLMResponse(text='{"summary": "ok"}', usage=LLMUsage())
        return self._responses.pop(0)


class FakeSpecRegistry:
    """Resolves any name to a minimal yaml-only Specialization."""

    def resolve(self, name: str) -> Specialization:
        return Specialization(name=name)


@pytest.mark.asyncio
async def test_crew_runner_lead_plans_and_members_execute():
    crew = CrewSpec(
        name="mini_crew",
        lead="senior_developer",
        members=[CrewMember("senior_developer"), CrewMember("db_engineer"), CrewMember("qa_tester")],
    )
    # 1 lead-plan response + 2 member responses (db_engineer, qa_tester)
    llm = FakeLLMAdapter(
        [
            LLMResponse(
                text='```json\n{"assignments": ['
                '{"member": "db_engineer", "task": "design schema"},'
                '{"member": "qa_tester", "task": "write tests"}]}\n```'
            ),
            LLMResponse(text='```json\n{"summary": "schema designed"}\n```'),
            LLMResponse(text='```json\n{"summary": "tests written"}\n```'),
        ]
    )
    deps = StageDeps(
        config=None,
        llm=llm,
        extra={"seed_crews": {"mini_crew": crew}, "specialization_registry": FakeSpecRegistry()},
    )
    stage = crew_runner_stage(deps, {"crew": "mini_crew"})
    task = DevAITask(intent="build the orders service", repo="tesserix/x")

    result = await stage.execute(task)
    out = result.data["crew_output"]

    assert out["crew"] == "mini_crew"
    assert {a["member"] for a in out["assignments"]} == {"db_engineer", "qa_tester"}
    assert out["member_outputs"]["db_engineer"]["summary"] == "schema designed"
    assert out["member_outputs"]["qa_tester"]["summary"] == "tests written"
    # the A2A trail recorded plan + handoff/response pairs
    kinds = [m["message_type"] for m in out["trail"]]
    assert "plan" in kinds and kinds.count("handoff") == 2 and kinds.count("response") == 2
    assert task.agent_context["crew_trail"]  # persisted on the task


@pytest.mark.asyncio
async def test_crew_runner_falls_back_when_lead_gives_no_plan():
    crew = CrewSpec(
        name="mini_crew",
        lead="senior_developer",
        members=[CrewMember("senior_developer"), CrewMember("qa_tester")],
    )
    # lead returns no assignments → fallback assigns each non-lead member the intent
    llm = FakeLLMAdapter(
        [
            LLMResponse(text="I am not sure how to split this."),  # no JSON assignments
            LLMResponse(text='```json\n{"summary": "did the whole thing"}\n```'),  # qa_tester
        ]
    )
    deps = StageDeps(
        config=None,
        llm=llm,
        extra={"seed_crews": {"mini_crew": crew}, "specialization_registry": FakeSpecRegistry()},
    )
    stage = crew_runner_stage(deps, {"crew": "mini_crew"})
    task = DevAITask(intent="ship it", repo="tesserix/x")

    result = await stage.execute(task)
    out = result.data["crew_output"]
    # fallback = one assignment for the single non-lead member
    assert [a["member"] for a in out["assignments"]] == ["qa_tester"]


@pytest.mark.asyncio
async def test_crew_runner_no_crew_resolved():
    deps = StageDeps(config=None, llm=None, extra={"seed_crews": {}})
    stage = crew_runner_stage(deps, {})
    result = await stage.execute(DevAITask(intent="x"))
    assert result.data.get("crew_error") == "no_crew"
