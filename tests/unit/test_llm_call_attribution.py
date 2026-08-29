"""A provider is not an agent.

`agent_executions` is the per-agent analytics table. Attributing an
unattributable LLM call to its provider filled it with rows named
`vertex_gemini` / `groq` / `anthropic`, which then showed up as agents in
every per-agent rollup.
"""

from __future__ import annotations

import pytest

from devai.pipeline.service import PipelineService


class _RecordingDB:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def record_llm_call(self, **kwargs) -> None:
        self.calls.append(kwargs)


@pytest.fixture
def db(monkeypatch) -> _RecordingDB:
    recorder = _RecordingDB()

    async def get_global_db():
        return recorder

    monkeypatch.setattr("devai.services.database.get_global_db", get_global_db)
    return recorder


async def _persist(**overrides):
    payload = {
        "run_id": "devai-1",
        "agent": "",
        "provider": "vertex_gemini",
        "model": "gemini-2.5-flash",
        "tok_in": 10,
        "tok_out": 20,
        "cost": 0.01,
        "tenant_id": "",
        "user_id": "",
        "triggered_by": "",
    }
    payload.update(overrides)
    await PipelineService._persist_turn_execution(**payload)


@pytest.mark.asyncio
async def test_unattributed_call_is_not_named_after_its_provider(db):
    await _persist(agent="")

    assert db.calls[0]["agent_name"] != "vertex_gemini"
    assert db.calls[0]["provider"] == "vertex_gemini"


@pytest.mark.asyncio
async def test_cost_is_still_recorded_for_an_unattributed_call(db):
    await _persist(agent="")

    assert db.calls[0]["cost_usd"] == 0.01
    assert db.calls[0]["tokens_input"] == 10


@pytest.mark.asyncio
async def test_a_real_agent_keeps_its_name(db):
    await _persist(agent="requirements_analyst")

    assert db.calls[0]["agent_name"] == "requirements_analyst"
