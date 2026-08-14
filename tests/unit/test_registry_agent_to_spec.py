"""A published Agent comes back as a runnable Specialization.

The forward direction (`spec_to_agent_envelope`) is what the Create-Agent form
publishes; without the inverse, an agent authored in the UI can be listed but
never executed.
"""

from __future__ import annotations

import pytest

from devai.registry.mapping import agent_envelope_to_spec, spec_to_agent_envelope
from devai.specializations.base import LLMProvider, RiskLevel, Specialization

_ENVELOPE = {
    "apiVersion": "registry.tesserix.io/v1",
    "kind": "Agent",
    "metadata": {"name": "release-notes-writer", "tag": "v2"},
    "spec": {
        "title": "Release Notes Writer",
        "description": "Turns a diff into notes.",
        "systemPrompt": "You write release notes.",
        "model": {"provider": "claude", "name": "claude-sonnet-4-20250514", "temperature": 0.2},
        "category": "specialist",
        "riskLevel": "low",
        "outputKey": "notes",
        "maxTurns": 12,
        "timeout": 300,
        "builtinTools": ["read_file"],
        "skills": ["changelog"],
        "mcpServers": ["github"],
    },
}


def test_a_published_agent_becomes_a_runnable_specialization() -> None:
    spec = agent_envelope_to_spec(_ENVELOPE)

    assert spec.name == "release_notes_writer"
    assert spec.display_name == "Release Notes Writer"
    assert spec.system_prompt == "You write release notes."
    assert spec.llm_provider is LLMProvider.CLAUDE
    assert spec.llm_model == "claude-sonnet-4-20250514"
    assert spec.temperature == 0.2
    assert spec.risk_level is RiskLevel.LOW
    assert spec.output_key == "notes"
    assert spec.max_turns == 12
    assert spec.timeout_seconds == 300
    assert spec.allowed_tools == ["read_file"]


def test_registry_composition_survives_the_round_trip() -> None:
    spec = agent_envelope_to_spec(_ENVELOPE)

    assert spec.metadata["registry_skills"] == ["changelog"]
    assert spec.metadata["registry_mcp_servers"] == ["github"]
    assert spec.metadata["registry_name"] == "release-notes-writer"
    assert spec.metadata["registry_tag"] == "v2"


def test_a_sparse_envelope_still_yields_a_usable_spec() -> None:
    spec = agent_envelope_to_spec({"metadata": {"name": "tiny"}, "spec": {}})

    assert spec.name == "tiny"
    assert spec.llm_provider is LLMProvider.AUTO
    assert spec.output_key == "tiny_output"


def test_the_flattened_shape_the_client_hands_back_is_accepted_too() -> None:
    # RegistryClient._unwrap projects metadata up and keeps `raw` flat, so the
    # mapping has to read what the client actually returns.
    spec = agent_envelope_to_spec(
        {
            "name": "release-notes-writer",
            "version": "v2",
            "title": "Release Notes Writer",
            "systemPrompt": "You write release notes.",
            "model": {"provider": "claude"},
            "skills": ["changelog"],
        }
    )

    assert spec.name == "release_notes_writer"
    assert spec.display_name == "Release Notes Writer"
    assert spec.llm_provider is LLMProvider.CLAUDE
    assert spec.metadata["registry_skills"] == ["changelog"]
    assert spec.metadata["registry_tag"] == "v2"


def test_an_envelope_without_a_name_is_refused() -> None:
    with pytest.raises(ValueError, match="name"):
        agent_envelope_to_spec({"spec": {"systemPrompt": "hi"}})


def test_a_name_that_cannot_be_a_role_is_refused() -> None:
    with pytest.raises(ValueError):
        agent_envelope_to_spec({"metadata": {"name": "9-lives"}, "spec": {}})


def test_publishing_a_spec_and_reading_it_back_preserves_what_runs_it() -> None:
    original = Specialization(
        name="db_engineer",
        display_name="DB Engineer",
        description="Owns migrations.",
        system_prompt="You write migrations.",
        llm_provider=LLMProvider.CLAUDE,
        llm_model="claude-sonnet-4-20250514",
        risk_level=RiskLevel.HIGH,
        allowed_tools=["run_sql"],
        max_turns=7,
    )

    restored = agent_envelope_to_spec(spec_to_agent_envelope(original))

    assert restored.name == original.name
    assert restored.system_prompt == original.system_prompt
    assert restored.llm_provider is original.llm_provider
    assert restored.llm_model == original.llm_model
    assert restored.risk_level is original.risk_level
    assert restored.allowed_tools == original.allowed_tools
    assert restored.max_turns == original.max_turns
