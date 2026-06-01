"""Additional authoring coverage: upsert semantics, fields→YAML, end-to-end."""

from __future__ import annotations

from devai.adapters.llm.base import LLMAdapter, LLMResponse
from devai.authoring import create_authoring_service
from devai.authoring.store import InMemoryDefinitionStore
from devai.config import Settings
from devai.pipeline.interfaces import StageDeps
from devai.pipeline.stages.specialization import run_specialization_stage
from devai.pipeline.types import DevAITask
from devai.specializations.registry import SpecializationRegistry

_FIELDS = {
    "name": "triage_agent",
    "display_name": "Triage Agent",
    "description": "triage incoming issues",
    "category": "planning",
    "llm_provider": "anthropic",
    "system_prompt": "You triage issues.",
    "allowed_tools": ["scm_list_files", "scm_create_issue"],
    "handover_schema": {"label": {"type": "string", "required": True}},
    "risk_level": "low",
}


async def test_upsert_preserves_created_at_and_bumps_updated_at():
    store = InMemoryDefinitionStore()
    svc = create_authoring_service(spec_registry=SpecializationRegistry())
    svc._store = store

    await svc.create_specialization_from_fields(_FIELDS)
    first = (await svc.list_specializations())[0]

    # Re-create with the same name → update, not duplicate.
    fields2 = {**_FIELDS, "description": "updated"}
    await svc.create_specialization_from_fields(fields2)
    rows = await svc.list_specializations()
    assert len(rows) == 1  # no duplicate
    second = rows[0]
    assert second["created_at"] == first["created_at"]
    assert second["updated_at"] >= first["updated_at"]


async def test_fields_produce_valid_runnable_spec():
    registry = SpecializationRegistry()
    svc = create_authoring_service(spec_registry=registry)
    await svc.create_specialization_from_fields(_FIELDS)

    spec = registry.resolve("triage_agent")
    assert spec.display_name == "Triage Agent"
    assert spec.category == "planning"
    assert spec.allowed_tools == ["scm_list_files", "scm_create_issue"]
    assert "label" in spec.handover_schema
    # output_key defaulted by the loader from the name.
    assert spec.output_key == "triage_agent_output"


class _ScriptedLLM(LLMAdapter):
    provider_name = "scripted"

    def __init__(self, text):
        self._text = text

    async def generate(self, request):  # type: ignore[override]
        return LLMResponse(text=self._text)


async def test_authored_agent_is_runnable_in_a_blueprint_stage():
    """The headline guarantee: an agent created via the authoring API can be
    resolved and executed by the run_specialization stage immediately."""
    registry = SpecializationRegistry()
    svc = create_authoring_service(spec_registry=registry)
    await svc.create_specialization_from_fields(_FIELDS)

    # And a blueprint that references it validates + persists.
    bp = (
        "name: triage-flow\n"
        "stages:\n"
        "  - name: t\n"
        "    stage: run_specialization\n"
        "    config:\n"
        "      specialization: triage_agent\n"
    )
    out = await svc.create_blueprint(bp)
    assert out["name"] == "triage-flow"

    # Run the stage against the live registry — it must resolve + execute.
    deps = StageDeps(
        config=Settings(),
        llm=_ScriptedLLM('```json\n{"label": "bug"}\n```'),
        extra={"specialization_registry": registry},
    )
    stage = run_specialization_stage(deps, {"specialization": "triage_agent"})
    result = await stage.execute(DevAITask(intent="something is broken"))
    assert result.data["triage_agent_output"] == {"label": "bug"}


async def test_delete_then_absent():
    svc = create_authoring_service(spec_registry=SpecializationRegistry())
    await svc.create_specialization_from_fields(_FIELDS)
    assert await svc.delete_specialization("triage_agent") is True
    assert await svc.get_specialization("triage_agent") is None
    # Deleting a second time is a no-op (False), not an error.
    assert await svc.delete_specialization("triage_agent") is False
