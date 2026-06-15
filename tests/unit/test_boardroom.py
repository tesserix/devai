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
    llm_model_boardroom_panel = ""
    llm_model_boardroom_moderator = ""


class _ScriptedLLM:
    """Returns canned text per call; raises where the script says 'RAISE'.
    Records the per-call request.model so tests can assert provider routing."""

    def __init__(self, script: list[str], provider_name: str = "fake"):
        self.script = script
        self.calls = 0
        self.models: list[str] = []
        self.provider_name = provider_name

    async def generate(self, request):
        self.calls += 1
        self.models.append(getattr(request, "model", "") or "")
        text = self.script.pop(0) if self.script else "POSITION: fine\nCHALLENGE: none\nCONCEDE: nothing"
        if text == "RAISE":
            raise RuntimeError("panelist offline")

        class _R:
            pass

        r = _R()
        r.text = text
        return r


class _AlwaysRaiseLLM:
    """Every seat call fails — exercises the zero-positions degrade path."""

    provider_name = "fake"

    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, request):
        self.calls += 1
        raise RuntimeError("provider outage")


def _stage(llm, config=None, cfg=None):
    deps = StageDeps(config=cfg or _Cfg(), llm=llm)
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
    # Round 1 can NEVER be consensus (nothing on the table to challenge yet);
    # round 2 with no challenges ends the 3-round debate early.
    llm = _ScriptedLLM(
        [agree] * 4
        + ["AGREED: stack\nDISPUTED: nothing"]
        + ['{"recruit": []}']  # supervisor declines to recruit
        + [agree] * 4
        + ["AGREED: stack\nDISPUTED: nothing", "## Decision\nNext.js + Postgres\n## Dissent\nnone"]
    )
    s = _stage(llm, {"rounds": "3"})
    task = DevAITask(intent="pick the stack", blueprint="b", repo="o/r")
    result = await s.execute(task)

    assert result.data["boardroom_consensus"] is True
    assert "Next.js" in result.data["technical_plan"]
    assert result.data["boardroom_decision"].startswith("## Decision")
    # Minutes: convened + (4 positions + synthesis) × 2 rounds + decision.
    assert len(result.data["a2a_messages"]) == 12
    # Early exit after round 2: 11 calls, not 3 full rounds' worth.
    assert llm.calls == 12


@pytest.mark.asyncio
async def test_deadlock_runs_all_rounds_and_records_dissent():
    fight = "POSITION: my way\nCHALLENGE: the architect is wrong about coupling\nCONCEDE: nothing"
    script = [fight] * 4 + ["AGREED: little\nDISPUTED: architecture (everyone)"]
    script += ['{"recruit": []}']  # supervisor declines to recruit
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
    assert result.data.get("boardroom_skip_reason") == "no_llm"


@pytest.mark.asyncio
async def test_non_claude_provider_runs_on_provider_default_model():
    """A user on OpenAI/Gemini/Groq must NOT get a forced claude-* id — the
    debate runs on their provider's own default model (model='')."""

    class _OpenAICfg(_Cfg):
        llm_model_boardroom_panel = "claude-haiku-4-5-20251001"
        llm_model_boardroom_moderator = "claude-sonnet-4-6"

    ok = "POSITION: fine\nCHALLENGE: none — I agree with the table\nCONCEDE: nothing"
    llm = _ScriptedLLM(
        [ok] * 4 + ["AGREED: all\nDISPUTED: nothing", "## Decision\nok\n## Dissent\nnone"],
        provider_name="openai",
    )
    s = _stage(llm, {"rounds": "1"}, cfg=_OpenAICfg())
    result = await s.execute(s_task := DevAITask(intent="pick stack", blueprint="b", repo="o/r"))
    assert s_task is not None
    # The configured models are Claude — but the resolved provider is OpenAI,
    # so EVERY call must defer to the provider default (empty model id).
    assert llm.models, "expected the debate to actually call the LLM"
    assert all(m == "" for m in llm.models), f"forced a cross-provider model: {llm.models}"
    assert result.data["boardroom_decision"].startswith("## Decision")


@pytest.mark.asyncio
async def test_claude_provider_uses_tiered_models():
    """Anthropic users keep the cheap-panel / strong-moderator split."""

    class _ClaudeCfg(_Cfg):
        llm_model_boardroom_panel = "claude-haiku-4-5-20251001"
        llm_model_boardroom_moderator = "claude-sonnet-4-6"

    ok = "POSITION: fine\nCHALLENGE: none — I agree with the table\nCONCEDE: nothing"
    llm = _ScriptedLLM(
        [ok] * 4 + ["AGREED: all\nDISPUTED: nothing", "## Decision\nok\n## Dissent\nnone"],
        provider_name="anthropic",
    )
    s = _stage(llm, {"rounds": "1"}, cfg=_ClaudeCfg())
    await s.execute(DevAITask(intent="pick stack", blueprint="b", repo="o/r"))
    # First 4 calls are panel seats (haiku); the synthesis + decision use sonnet.
    assert llm.models[:4] == ["claude-haiku-4-5-20251001"] * 4
    assert llm.models[-1] == "claude-sonnet-4-6"


@pytest.mark.asyncio
async def test_zero_positions_degrades_not_raises():
    """Every seat failing must NOT raise (→ recovery/runbook/needs-human); it
    degrades to a visible skip and writes no hollow decision."""
    s = _stage(_AlwaysRaiseLLM(), {"rounds": "1"})
    result = await s.execute(DevAITask(intent="anything", blueprint="b", repo="o/r"))
    assert result.data.get("boardroom_skipped") is True
    assert result.data.get("boardroom_skip_reason") == "panel_unreachable"
    # No hollow plan leaks downstream.
    assert "boardroom_decision" not in result.data
    assert "technical_plan" not in result.data
