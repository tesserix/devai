"""The loop the agent platform exists for, end to end through the HTTP surface.

Author an agent (it lands in the registry) → create a sandbox pinning it →
invoke it → read the trace. Every step here is the real component: the
specialization service, the sandbox service, the invoker and the routes. Only
the registry itself and the model are stood in for, because they are the two
things DevAI does not own.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from devai.adapters.llm.base import LLMAdapter, LLMResponse, LLMUsage
from devai.config import Settings
from devai.pipeline.interfaces import StageDeps
from devai.sandbox.evals import EvalRunner, EvalStore
from devai.sandbox.invoke import SandboxInvoker
from devai.sandbox.routes import router
from devai.sandbox.service import SandboxService
from devai.sandbox.trace import TraceStore
from devai.specializations.service import SpecializationService
from tests.unit.test_sandbox import _FakeDB

_SAM = {"X-Forwarded-Email": "sam@example.com"}


class _Catalog:
    """The registry, as far as DevAI can see it: a list that publishing grows."""

    def __init__(self) -> None:
        self.agents: list[dict[str, Any]] = []

    def list_skills(self) -> list[Any]:
        return []

    def list_agents(self) -> list[Any]:
        return [_Published(a) for a in self.agents]


class _Published:
    def __init__(self, raw: dict[str, Any]) -> None:
        self.name = raw.get("name", "")
        self.raw = raw


class _ScriptedLLM(LLMAdapter):
    provider_name = "scripted"

    async def generate(self, request):  # type: ignore[override]
        return LLMResponse(
            text="v2.1 ships the sandbox console.",
            usage=LLMUsage(prompt_tokens=90, completion_tokens=12),
        )


class _GrantedTestCredential:
    def __init__(self, llm: LLMAdapter) -> None:
        self._llm = llm

    async def resolve(self, record, deps):
        assert record.spec.credentials.llm_connector == "sandbox-test"
        assert record.spec.credentials.confirmed is True
        return replace(
            deps,
            llm=self._llm,
            scm=None,
            memory=None,
            secrets=None,
            settings_service=None,
            llm_resolver=None,
            scm_resolver=None,
            extra=None,
        )


@pytest.fixture
async def platform(tmp_path: Path):
    catalog = _Catalog()
    specs = SpecializationService(Settings(specializations_dir=str(tmp_path)), registry_client=catalog)
    await specs.start()

    app = FastAPI()
    app.state.specialization_service = specs
    app.state.sandbox_service = SandboxService(_FakeDB())
    app.state.sandbox_traces = TraceStore(None)
    llm = _ScriptedLLM()
    app.state.sandbox_invoker = SandboxInvoker(
        specializations=specs,
        deps=StageDeps(config=Settings(), llm=llm),
        traces=app.state.sandbox_traces,
        credentials=_GrantedTestCredential(llm),
    )
    app.state.sandbox_evals = EvalRunner(app.state.sandbox_invoker, EvalStore(None))
    app.include_router(router)
    with TestClient(app) as client:
        yield client, catalog


async def test_a_draft_agent_is_testable_before_it_is_published(platform) -> None:
    # The studio's middle step: the catalog is still empty here, and the run works.
    client, catalog = platform

    created = client.post(
        "/api/sandboxes",
        headers=_SAM,
        json={
            "agent": {"name": "draft-writer", "version": "draft"},
            "model": {"provider": "anthropic", "model": "claude-sonnet-4-20250514"},
            "credentials": {"llm_connector": "sandbox-test", "confirmed": True},
            "draft": {
                "apiVersion": "registry.agentic.dev/v1alpha1",
                "kind": "Agent",
                "metadata": {"name": "draft-writer"},
                "spec": {"title": "Draft Writer", "systemPrompt": "You write release notes."},
            },
            "tools": {"default_mode": "mock"},
        },
    )
    assert created.status_code == 201, created.text
    assert catalog.agents == []

    answered = client.post(
        f"/api/sandboxes/{created.json()['id']}/invoke",
        headers=_SAM,
        json={"message": "summarise the release"},
    )
    assert answered.status_code == 200, answered.text
    assert answered.json()["final_text"] == "v2.1 ships the sandbox console."


async def test_a_draft_is_scored_against_its_checks_before_it_is_published(platform) -> None:
    # The studio's gate: a suite, not a single chat turn, is what says "publish".
    client, _ = platform

    created = client.post(
        "/api/sandboxes",
        headers=_SAM,
        json={
            "agent": {"name": "draft-writer", "version": "draft"},
            "model": {"provider": "anthropic", "model": "claude-sonnet-4-20250514"},
            "credentials": {"llm_connector": "sandbox-test", "confirmed": True},
            "draft": {
                "kind": "Agent",
                "metadata": {"name": "draft-writer"},
                "spec": {"systemPrompt": "You write release notes."},
            },
        },
    )
    sandbox_id = created.json()["id"]

    scored = client.post(
        f"/api/sandboxes/{sandbox_id}/evals",
        headers=_SAM,
        json={
            "cases": [
                {"name": "names the version", "input": "summarise", "expect": {"contains": ["v2.1"]}},
                {"name": "keeps it short", "input": "summarise", "expect": {"max_total_tokens": 10}},
            ]
        },
    )

    assert scored.status_code == 200, scored.text
    body = scored.json()
    assert body["summary"]["passed"] == 1
    assert body["summary"]["failed"] == 1
    assert body["results"][1]["failures"] == ["used 102 tokens, budget 10"]


async def test_an_agent_authored_now_can_be_sandboxed_invoked_and_read_back(platform) -> None:
    client, catalog = platform

    # 1. Authoring publishes to the registry.
    catalog.agents.append(
        {
            "name": "release-notes-writer",
            "displayName": "Release Notes Writer",
            "systemPrompt": "You write release notes.",
        }
    )

    # 2. A sandbox pins that agent, a model and a tool policy.
    created = client.post(
        "/api/sandboxes",
        headers=_SAM,
        json={
            "agent": {"name": "release-notes-writer", "version": "v1"},
            "model": {"provider": "anthropic", "model": "claude-sonnet-4-20250514"},
            "credentials": {"llm_connector": "sandbox-test", "confirmed": True},
            "tools": {"default_mode": "mock"},
        },
    )
    assert created.status_code == 201, created.text
    sandbox_id = created.json()["id"]

    # 3. Invoking it answers, and says which trace it left.
    answered = client.post(
        f"/api/sandboxes/{sandbox_id}/invoke",
        headers=_SAM,
        json={"message": "summarise the release"},
    )
    assert answered.status_code == 200, answered.text
    body = answered.json()
    assert body["ok"] is True
    assert body["final_text"] == "v2.1 ships the sandbox console."
    assert body["totals"]["total_tokens"] == 102

    # 4. The trace is readable afterwards — that is where the metrics come from.
    listed = client.get(f"/api/sandboxes/{sandbox_id}/traces", headers=_SAM)
    assert [t["id"] for t in listed.json()] == [body["id"]]

    trace = client.get(f"/api/sandboxes/{sandbox_id}/traces/{body['id']}", headers=_SAM).json()
    assert [s["kind"] for s in trace["steps"]] == ["prompt", "prompt", "llm", "response"]
