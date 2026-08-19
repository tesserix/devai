"""HTTP layer for invoking a sandbox and reading its traces."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from devai.sandbox.invoke import SandboxInvoker
from devai.sandbox.routes import router
from devai.sandbox.service import SandboxService
from devai.sandbox.trace import TraceStore
from devai.specializations.loader import load_specialization_from_string
from devai.specializations.registry import SpecializationRegistry
from tests.unit.test_sandbox import _FakeDB
from tests.unit.test_sandbox_invoke import _GrantedSandboxLLM, _ScriptedLLM

_SAM = {"X-Forwarded-Email": "sam@example.com"}
_MALLORY = {"X-Forwarded-Email": "mallory@example.com"}
_SPEC: dict[str, Any] = {
    "agent": {"name": "release-notes-writer", "version": "v1"},
    "model": {"provider": "anthropic", "model": "claude-sonnet-4-20250514"},
}
_YAML = """
name: release_notes_writer
llm_provider: claude
system_prompt: You write release notes.
"""


class _Specs:
    """Stands in for SpecializationService — the invoker only resolves roles."""

    def __init__(self, registry: SpecializationRegistry) -> None:
        self._registry = registry

    async def resolve_runnable(self, name: str) -> Any:
        return self._registry.resolve(name) if self._registry.has(name) else None


def _client(*, invoker: bool = True, responses: list[Any] | None = None) -> TestClient:
    from devai.adapters.llm.base import LLMResponse
    from devai.config import Settings
    from devai.pipeline.interfaces import StageDeps

    registry = SpecializationRegistry()
    registry.register(load_specialization_from_string(_YAML))
    specs = _Specs(registry)

    app = FastAPI()
    app.state.sandbox_service = SandboxService(_FakeDB())
    app.state.sandbox_traces = TraceStore(None)
    llm = _ScriptedLLM(responses or [LLMResponse(text="Here are the notes.")])
    app.state.sandbox_invoker = (
        SandboxInvoker(
            specializations=specs,
            deps=StageDeps(config=Settings(), llm=llm),
            traces=app.state.sandbox_traces,
            credentials=_GrantedSandboxLLM(llm),
        )
        if invoker
        else None
    )
    app.include_router(router)
    return TestClient(app)


def _sandbox(client: TestClient) -> str:
    return client.post("/api/sandboxes", json=_SPEC, headers=_SAM).json()["id"]


def test_invoke_returns_the_answer_and_a_trace_id() -> None:
    client = _client()
    sid = _sandbox(client)

    r = client.post(f"/api/sandboxes/{sid}/invoke", json={"message": "summarise"}, headers=_SAM)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["final_text"] == "Here are the notes."
    assert body["id"].startswith("inv-")
    assert body["totals"]["llm_calls"] == 1


def test_the_trace_is_listed_and_readable_afterwards() -> None:
    client = _client()
    sid = _sandbox(client)
    inv_id = client.post(f"/api/sandboxes/{sid}/invoke", json={"message": "go"}, headers=_SAM).json()["id"]

    listed = client.get(f"/api/sandboxes/{sid}/traces", headers=_SAM)
    one = client.get(f"/api/sandboxes/{sid}/traces/{inv_id}", headers=_SAM)

    assert [t["id"] for t in listed.json()] == [inv_id]
    assert [s["kind"] for s in one.json()["steps"]][0] == "prompt"


def test_an_empty_message_is_refused() -> None:
    client = _client()
    sid = _sandbox(client)

    assert client.post(f"/api/sandboxes/{sid}/invoke", json={"message": "  "}, headers=_SAM).status_code == 422


def test_another_owner_can_neither_invoke_nor_read_traces() -> None:
    client = _client()
    sid = _sandbox(client)
    client.post(f"/api/sandboxes/{sid}/invoke", json={"message": "go"}, headers=_SAM)

    assert client.post(f"/api/sandboxes/{sid}/invoke", json={"message": "go"}, headers=_MALLORY).status_code == 404
    assert client.get(f"/api/sandboxes/{sid}/traces", headers=_MALLORY).status_code == 404


def test_an_unknown_trace_is_404() -> None:
    client = _client()
    sid = _sandbox(client)

    assert client.get(f"/api/sandboxes/{sid}/traces/inv-nope", headers=_SAM).status_code == 404


def test_invoke_503s_until_the_invoker_is_wired() -> None:
    client = _client(invoker=False)
    sid = _sandbox(client)

    assert client.post(f"/api/sandboxes/{sid}/invoke", json={"message": "go"}, headers=_SAM).status_code == 503


def test_an_agent_the_platform_cannot_run_is_a_422_not_a_500() -> None:
    client = _client()
    sid = client.post(
        "/api/sandboxes",
        json={**_SPEC, "agent": {"name": "not-published", "version": "v1"}},
        headers=_SAM,
    ).json()["id"]

    r = client.post(f"/api/sandboxes/{sid}/invoke", json={"message": "go"}, headers=_SAM)

    assert r.status_code == 422
    assert "not runnable" in r.json()["detail"]
