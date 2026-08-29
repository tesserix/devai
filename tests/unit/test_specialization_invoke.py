"""Tests for SpecializationService.invoke() — the Job-runner execution surface.

`invoke()` is what `devai.runner.entrypoint` calls. It runs a YAML spec through
the Agent SDK (SpecAgent → AgentRunner) and returns its patch, or None for a
legacy/unknown agent so the caller falls back to the legacy class. This closes
the long-standing gap where a YAML-only role could not run as a Job at all.
"""

from __future__ import annotations

from devai.adapters.llm.base import LLMResponse, LLMUsage
from devai.config import Settings
from devai.pipeline.interfaces import StageDeps
from devai.specializations.loader import load_specialization_from_string
from devai.specializations.registry import SpecializationRegistry
from devai.specializations.service import SpecializationService

_YAML = """
name: job_yaml
category: planning
allowed_tools: []
output_key: job_yaml_output
handover_schema:
  summary:
    type: string
    required: true
system_prompt: do the job
"""

_LEGACY = """
name: job_legacy
category: coding
legacy_python_class: devai.agents.senior_developer.SeniorDeveloperAgent
output_key: job_legacy_output
system_prompt: bridged
"""


class _ScriptedLLM:
    provider_name = "scripted"

    def __init__(self, text: str) -> None:
        self._text = text

    async def generate(self, request):  # noqa: ANN001
        return LLMResponse(
            text=self._text,
            usage=LLMUsage(prompt_tokens=7, completion_tokens=3),
        )


def _service(*specs: str) -> SpecializationService:
    reg = SpecializationRegistry()
    for spec in specs:
        reg.register(load_specialization_from_string(spec))
    svc = SpecializationService(Settings())
    svc._registry = reg
    svc._started = True
    return svc


async def test_invoke_runs_yaml_spec_via_sdk():
    svc = _service(_YAML)
    deps = StageDeps(config=Settings(), llm=_ScriptedLLM('```json\n{"summary": "ran in a job"}\n```'))

    patch = await svc.invoke("job_yaml", {"run_id": "devai-x", "requirements": "build"}, deps=deps)

    assert patch is not None
    assert patch["summary"] == "ran in a job"
    assert patch["ok"] is True
    assert patch["final_text"] == '```json\n{"summary": "ran in a job"}\n```'
    assert patch["usage"] == {
        "prompt_tokens": 7,
        "completion_tokens": 3,
        "total_tokens": 10,
        "tool_calls": 0,
        "turns": 1,
    }
    # The raw text is surfaced under <name>_text for display/debug.
    assert "job_yaml_text" in patch


async def test_invoke_returns_none_for_legacy_class():
    # A spec that bridges a Python class is NOT run here — invoke returns None so
    # the entrypoint constructs the legacy class directly (no reflection here).
    svc = _service(_LEGACY)
    assert await svc.invoke("job_legacy", {"run_id": "x"}, deps=StageDeps(config=Settings())) is None


async def test_invoke_returns_none_for_unknown_agent():
    svc = _service(_YAML)
    assert await svc.invoke("ghost", {"run_id": "x"}, deps=StageDeps(config=Settings())) is None


def test_task_from_state_maps_fields_and_context():
    principal = {
        "email": "u@example.com",
        "uid": "user-1",
        "tenant_id": "tenant-a",
    }
    task = SpecializationService._task_from_state(
        {
            "run_id": "devai-abc",
            "requirements": "ship it",
            "repo_full_name": "o/r",
            "trigger_actor": "u@example.com",
            "trace_id": "t1",
            "blueprint": "alm-pipeline",
            "principal": principal,
            "team_id": "team-a",
            "upstream_output": "PRIOR",  # non-reserved → handover context
        }
    )
    assert task.id == "devai-abc"
    assert task.intent == "ship it"
    assert task.repo == "o/r"
    assert task.triggered_by == "u@example.com"
    assert task.principal == principal
    assert task.team_id == "team-a"
    assert task.agent_context["upstream_output"] == "PRIOR"
    # Reserved keys don't leak into the handover bag.
    assert "requirements" not in task.agent_context
    assert "principal" not in task.agent_context
    assert "team_id" not in task.agent_context
