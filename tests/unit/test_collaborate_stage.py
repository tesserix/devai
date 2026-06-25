"""Tests for the `collaborate` stage — collaboration patterns from a blueprint.

Exercises the stage with injected YAML specs + an LLM that answers per-agent
(keyed on the request's stamped `extra["agent"]`, so concurrent mixture runs are
deterministic regardless of scheduling order).
"""

from __future__ import annotations

from devai.adapters.llm.base import LLMResponse
from devai.config import Settings
from devai.pipeline.interfaces import StageDeps
from devai.pipeline.stages.collaborate import collaborate_stage
from devai.pipeline.types import DevAITask
from devai.specializations.loader import load_specialization_from_string
from devai.specializations.registry import SpecializationRegistry


def _spec(name: str, field: str, ftype: str = "string") -> str:
    return (
        f"name: {name}\n"
        f"allowed_tools: []\n"
        f"output_key: {name}_output\n"
        f"handover_schema:\n  {field}:\n    type: {ftype}\n    required: true\n"
        f"system_prompt: {name}\n"
    )


class _ByAgentLLM:
    """Answers each generate() with the JSON scripted for that request's agent."""

    provider_name = "scripted"

    def __init__(self, by_agent: dict[str, str]) -> None:
        self._by_agent = by_agent
        self.calls = 0

    async def generate(self, request):  # noqa: ANN001
        self.calls += 1
        agent = (request.extra or {}).get("agent", "")
        return LLMResponse(text=self._by_agent.get(agent, '{"summary": "x"}'))


def _deps(llm, *specs: str) -> StageDeps:
    reg = SpecializationRegistry()
    for spec in specs:
        reg.register(load_specialization_from_string(spec))
    return StageDeps(config=Settings(), llm=llm, extra={"specialization_registry": reg})


async def test_mixture_fans_out_and_aggregates():
    llm = _ByAgentLLM({"alpha": '{"v": "A"}', "beta": '{"v": "B"}'})
    deps = _deps(llm, _spec("alpha", "v"), _spec("beta", "v"))
    stage = collaborate_stage(deps, {"pattern": "mixture", "agents": "alpha,beta", "output_key": "analysis"})

    result = await stage.execute(DevAITask(intent="x"))

    bag = result.data["analysis"]
    assert bag["alpha_output"]["v"] == "A"
    assert bag["beta_output"]["v"] == "B"
    assert llm.calls == 2


async def test_deliberation_loops_until_critic_approves():
    # The critic approves once it sees the actor's draft → one round.
    llm = _ByAgentLLM({"drafter": '{"draft": "v1"}', "judge": '{"approved": true}'})
    deps = _deps(llm, _spec("drafter", "draft"), _spec("judge", "approved", "boolean"))
    stage = collaborate_stage(
        deps,
        {"pattern": "deliberation", "actor": "drafter", "critic": "judge", "output_key": "review"},
    )

    result = await stage.execute(DevAITask(intent="x"))

    review = result.data["review"]
    assert review["draft"] == "v1"
    assert review["_deliberation_approved"] is True


async def test_distillation_escalates_to_expert_on_learner_escalate_flag():
    llm = _ByAgentLLM({"cheap": '{"answer": "unsure", "escalate": true}', "smart": '{"answer": "definitive"}'})
    deps = _deps(llm, _spec("cheap", "answer"), _spec("smart", "answer"))
    stage = collaborate_stage(deps, {"pattern": "distillation", "learner": "cheap", "expert": "smart"})

    result = await stage.execute(DevAITask(intent="x"))

    out = result.data["collaborate_distillation_output"]
    assert out["answer"] == "definitive"
    assert out["_escalated_from"] == "cheap"


async def test_missing_config_skips_with_clear_error():
    stage = collaborate_stage(_deps(_ByAgentLLM({})), {"pattern": "deliberation"})  # no actor/critic
    result = await stage.execute(DevAITask(intent="x"))
    assert "skipped" in result.message
    assert "collaborate_deliberation_output_error" in result.data


async def test_unknown_pattern_skips():
    stage = collaborate_stage(_deps(_ByAgentLLM({})), {"pattern": "telepathy"})
    result = await stage.execute(DevAITask(intent="x"))
    assert "unknown collaboration pattern" in result.message


def test_collaborate_stage_is_registered():
    from types import SimpleNamespace

    from devai.blueprint.registry import StageRegistry, register_defaults

    reg = StageRegistry()
    register_defaults(reg)
    assert reg.has("collaborate")
    stage = reg.resolve("collaborate", StageDeps(config=SimpleNamespace()), {"pattern": "mixture"})
    assert stage.name() == "collaborate:mixture"
