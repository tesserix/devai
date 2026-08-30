"""A promptRef-only draft must run with the referenced prompt, not an empty one."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from devai.sandbox.models import SandboxSpec
from devai.sandbox.service import SandboxError, SandboxService

_PROMPT_TEXT = "You are the Release Concierge. Always end with INTEGRATION-OK."


def _draft(spec_overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "apiVersion": "registry.agentic.dev/v1alpha1",
        "kind": "Agent",
        "metadata": {"name": "draft-agent"},
        "spec": {
            "title": "Draft Agent",
            "model": {"provider": "vertex_gemini", "name": "gemini-2.5-flash"},
            "systemPrompt": "",
            "promptRef": "release-concierge-prompt",
            **(spec_overrides or {}),
        },
    }


def _spec(draft: dict[str, Any]) -> SandboxSpec:
    return SandboxSpec.model_validate(
        {
            "agent": {"name": "draft-agent", "version": "draft"},
            "model": {"provider": "vertex_gemini", "model": "gemini-2.5-flash"},
            "draft": draft,
        }
    )


class _FakeDB:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    async def create_sandbox(self, **kw: Any) -> dict[str, Any]:
        self.rows[kw["sandbox_id"]] = {**kw, "id": kw["sandbox_id"]}
        return self.rows[kw["sandbox_id"]]

    async def get_sandbox(self, sandbox_id: str) -> dict[str, Any] | None:
        return self.rows.get(sandbox_id)

    async def set_sandbox_status(self, sandbox_id: str, status: str, detail: dict[str, Any] | None = None) -> None:
        self.rows[sandbox_id]["status"] = status

    async def touch_sandbox(self, sandbox_id: str) -> None:
        self.rows[sandbox_id]["last_access_at"] = datetime.now(UTC)


class _FakeRegistry:
    def __init__(self, prompt_text: str | None = _PROMPT_TEXT) -> None:
        self._prompt_text = prompt_text

    def artifact_exists(self, plural: str, name: str) -> bool | None:
        return True

    def get_artifact_envelope(self, plural: str, name: str) -> dict[str, Any] | None:
        if plural != "prompts" or self._prompt_text is None:
            return None
        return {"metadata": {"name": name}, "spec": {"systemPrompt": self._prompt_text}}


async def test_a_promptref_only_draft_gets_the_prompt_inlined() -> None:
    svc = SandboxService(_FakeDB(), registry=_FakeRegistry())

    rec = await svc.create(_spec(_draft()), owner="sam@example.com")

    assert rec.spec.draft["spec"]["systemPrompt"] == _PROMPT_TEXT


async def test_an_inline_system_prompt_is_left_alone() -> None:
    svc = SandboxService(_FakeDB(), registry=_FakeRegistry())
    draft = _draft({"systemPrompt": "Inline wins."})

    rec = await svc.create(_spec(draft), owner="sam@example.com")

    assert rec.spec.draft["spec"]["systemPrompt"] == "Inline wins."


async def test_a_promptref_that_resolves_to_nothing_is_refused() -> None:
    svc = SandboxService(_FakeDB(), registry=_FakeRegistry(prompt_text=None))

    with pytest.raises(SandboxError, match="release-concierge-prompt"):
        await svc.create(_spec(_draft()), owner="sam@example.com")


async def test_without_a_registry_the_draft_is_left_as_written() -> None:
    svc = SandboxService(_FakeDB())

    rec = await svc.create(_spec(_draft()), owner="sam@example.com")

    assert rec.spec.draft["spec"]["systemPrompt"] == ""
