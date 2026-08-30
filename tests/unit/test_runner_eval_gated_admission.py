"""Runner admission for eval-gated registry agents (user-authored catalog records)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from devai.evaluations.gates import EVAL_GATE_LABEL
from devai.runner.entrypoint import _invoke_eval_gated

_ENVELOPE = {
    "metadata": {"name": "measure-mate-agent", "tag": "1"},
    "spec": {
        "description": "Converts units.",
        "version": "1",
        "systemPrompt": "Convert units precisely.",
        "llm": {"provider": "vertex_gemini", "model": "gemini-2.5-flash"},
    },
}


@dataclass
class _Agent:
    name: str = "measure-mate-agent"
    version: str = "1"
    labels: dict[str, str] = field(default_factory=lambda: {EVAL_GATE_LABEL: "passed"})
    raw: dict[str, Any] = field(default_factory=lambda: dict(_ENVELOPE))


class _Registry:
    def __init__(self, agent: _Agent | None) -> None:
        self._agent = agent

    def get_agent(self, name: str) -> _Agent | None:
        return self._agent if self._agent and self._agent.name == name else None


class _Service:
    def __init__(self) -> None:
        self.specs: list[Any] = []

    async def invoke_spec(self, spec: Any, state: dict[str, Any]) -> dict[str, Any]:
        self.specs.append(spec)
        return {"ok": True, "final_text": "42 km"}


async def test_gated_registry_agent_is_admitted_and_run() -> None:
    service = _Service()
    patch = await _invoke_eval_gated("measure-mate-agent", {"run_id": "t1"}, service, _Registry(_Agent()))

    assert patch == {"ok": True, "final_text": "42 km"}
    assert service.specs and service.specs[0].metadata["registry_name"] == "measure-mate-agent"


async def test_unstamped_record_fails_closed() -> None:
    agent = _Agent(labels={})
    patch = await _invoke_eval_gated("measure-mate-agent", {}, _Service(), _Registry(agent))

    assert patch is not None and patch["ok"] is False
    assert "no passing eval gate" in patch["error"]


async def test_unknown_agent_fails_closed() -> None:
    patch = await _invoke_eval_gated("ghost-agent", {}, _Service(), _Registry(None))

    assert patch is not None and patch["ok"] is False
    assert "not admitted" in patch["error"]


async def test_missing_registry_client_fails_closed() -> None:
    patch = await _invoke_eval_gated("measure-mate-agent", {}, _Service(), None)

    assert patch is not None and patch["ok"] is False
    assert "registry unavailable" in patch["error"]
