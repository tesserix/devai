"""Boardroom debate stage — scenario coverage.

  - panel routing: core quartet always seated; topic keywords pull in
    specialists (data → DB engineer, deploy → infra, ...).
  - consensus: a round with no challenges ends the debate early.
  - deadlock: persistent challenges run all rounds; dissent survives into
    the decision document.
  - flaky panelist: an LLM error skips the seat, the meeting continues.
  - no LLM: the stage no-ops visibly and never blocks the pipeline.
  - outputs: technical_plan + boardroom_decision + a2a minutes land in the
    handover for downstream stages and the Timeline.
"""

from __future__ import annotations

import pytest

from devai.pipeline.interfaces import StageDeps
from devai.pipeline.stages.boardroom import _BoardroomDebateStage
from devai.pipeline.types import DevAITask


class _Cfg:
    pipeline_label = "x"


class _ScriptedLLM:
    """Returns canned text per call; raises where the script says 'RAISE'."""

    provider_name = "fake"

    def __init__(self, script: list[str]):
        self.script = script
        self.calls = 0

    async def generate(self, request):
        self.calls += 1
        text = self.script.pop(0) if self.script else "POSITION: fine\nCHALLENGE: none\nCONCEDE: nothing"
        if text == "RAISE":
            raise RuntimeError("panelist offline")

        class _R:
            pass

        r = _R()
        r.text = text
        return r


def _stage(llm, config=None):
    deps = StageDeps(config=_Cfg(), llm=llm)
    return _BoardroomDebateStage(deps, config or {})


def test_panel_routing_pulls_specialists():
    s = _stage(_ScriptedLLM([]))
    base = s._select_panel("decide the product direction")
    assert [r for r, _, _ in base] == [
        "product_director",
        "engineering_manager",
        "staff_architect",
        "security_expert",
    ]
    with_db = s._select_panel("choose the database schema and deploy strategy")
    roles = [r for r, _, _ in with_db]
    assert "db_engineer" in roles and "infra_provisioner" in roles


@pytest.mark.asyncio
async def test_consensus_round_ends_debate_early_and_outputs_plan():
    agree = "POSITION: Next.js + Postgres.\nCHALLENGE: none — I agree with the table\nCONCEDE: nothing"
    # 4 panelists agree in round 1 + moderator synthesis + final decision.
    llm = _ScriptedLLM([agree] * 4 + ["AGREED: stack\nDISPUTED: nothing", "## Decision\nNext.js + Postgres\n## Dissent\nnone"])
    s = _stage(llm, {"rounds": "3"})
    task = DevAITask(intent="pick the stack", blueprint="b", repo="o/r")
    result = await s.execute(task)

    assert result.data["boardroom_consensus"] is True
    assert "Next.js" in result.data["technical_plan"]
    assert result.data["boardroom_decision"].startswith("## Decision")
    # Minutes: convened + 4 positions + synthesis + decision.
    assert len(result.data["a2a_messages"]) == 7
    # Early exit: 6 calls total, not 3 rounds' worth.
    assert llm.calls == 6


@pytest.mark.asyncio
async def test_deadlock_runs_all_rounds_and_records_dissent():
    fight = "POSITION: my way\nCHALLENGE: the architect is wrong about coupling\nCONCEDE: nothing"
    script = []
    for _ in range(2):  # rounds
        script += [fight] * 4 + ["AGREED: little\nDISPUTED: architecture (everyone)"]
    script += ["## Decision\nmajority picks X\n## Dissent\nStaff Architect disagrees on coupling"]
    llm = _ScriptedLLM(script)
    s = _stage(llm, {"rounds": "2"})
    task = DevAITask(intent="architecture fight", blueprint="b", repo="o/r")
    result = await s.execute(task)

    assert result.data["boardroom_consensus"] is False
    assert "Dissent" in result.data["boardroom_decision"]
    assert "majority + recorded dissent" in result.message


@pytest.mark.asyncio
async def test_flaky_panelist_does_not_cancel_the_meeting():
    ok = "POSITION: fine\nCHALLENGE: none — I agree with the table\nCONCEDE: nothing"
    llm = _ScriptedLLM(["RAISE", ok, ok, ok, "AGREED: all\nDISPUTED: nothing", "## Decision\nok\n## Dissent\nnone"])
    s = _stage(llm, {"rounds": "1"})
    task = DevAITask(intent="simple call", blueprint="b", repo="o/r")
    result = await s.execute(task)

    assert "absent — LLM error" in result.data["boardroom_transcript"]
    assert result.data["boardroom_decision"]


@pytest.mark.asyncio
async def test_no_llm_skips_visibly():
    s = _stage(None)
    task = DevAITask(intent="anything", blueprint="b", repo="o/r")
    result = await s.execute(task)
    assert result.data.get("boardroom_skipped") is True
