"""Real review/security gating (Phase 0) — built on generic primitives.

The verdicts used to be strings nothing read, so a changes-requested review or a
security block sailed to deploy. Now a stage DERIVES a boolean gate flag (via the
generic ``flag_deriver`` / a thin ALM helper + the ``AgentStage`` deriver), a
bounded fix→re-check loop tries to resolve it, and the generic
``EnforceFlagsStage`` blocks delivery when any flag is still set — usable by any
blueprint with any flags, not just the ALM review/security gates.
"""

from __future__ import annotations

import pytest

from devai.agentruntime import AgentResult
from devai.pipeline.interfaces import StageDeps
from devai.pipeline.stages.agent_stage import AgentStage
from devai.pipeline.stages.alm import _derive_review_gate, _derive_security_gate
from devai.pipeline.stages.flow import EnforceFlagsStage, enforce_flags_stage, flag_deriver
from devai.pipeline.types import DevAITask


def _deps() -> StageDeps:
    return StageDeps(config=None, scm=None, state_manager=None, llm=None)


def _task() -> DevAITask:
    return DevAITask(intent="x", repo="tesserix/x")


# ─── derivers (ALM thin helpers + generic flag_deriver) ──────────────────────


def test_alm_derivers_flag_only_the_bad_verdict() -> None:
    assert _derive_review_gate({"review_decision": "changes_requested"}) == {"review_changes_requested": True}
    assert _derive_review_gate({"review_decision": "approved"}) == {"review_changes_requested": False}
    assert _derive_security_gate({"security_decision": "block"}) == {"security_blocked": True}
    assert _derive_security_gate({"security_decision": "pass"}) == {"security_blocked": False}


def test_generic_flag_deriver_from_config_spec() -> None:
    """Any blueprint can turn a verdict string into a gate flag with no Python."""
    derive = flag_deriver("review_changes_requested=review_decision:changes_requested")
    assert derive({"review_decision": "changes_requested"}) == {"review_changes_requested": True}
    assert derive({"review_decision": "approved"}) == {"review_changes_requested": False}
    assert derive({}) == {"review_changes_requested": False}


# ─── generic enforce gate ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_enforce_flags_blocks_when_any_flag_set() -> None:
    stage = EnforceFlagsStage(
        _deps(),
        flags=[("review_changes_requested", "review wants changes"), ("security_blocked", "security blocks")],
    )
    task = _task()
    task.agent_context["security_blocked"] = True
    with pytest.raises(RuntimeError, match="security blocks"):
        await stage.execute(task)


def test_enforce_flags_stage_parses_config() -> None:
    stage = enforce_flags_stage(_deps(), {"flags": "review_changes_requested:wants changes;security_blocked:blocks"})
    assert stage._flags == [("review_changes_requested", "wants changes"), ("security_blocked", "blocks")]


# ─── deriver wiring through AgentStage ───────────────────────────────────────


@pytest.mark.asyncio
async def test_agent_stage_folds_derived_flag_into_data(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stage's verdict becomes a boolean gate flag in the stage data, so it
    flows through merge_handover into agent_context for the condition."""

    class _Reviewer:
        name = "staff_reviewer"

        async def run(self, ctx):  # noqa: ANN001
            return AgentResult(ok=True, output_key="staff_reviewer", handover={"review_decision": "changes_requested"})

    async def _fake_resolve(deps, task, *, trial_gate, stage_name):  # noqa: ANN001
        return deps.config, deps.scm

    monkeypatch.setattr("devai.pipeline.stages.agent_stage.resolve_principal_run", _fake_resolve)

    stage = AgentStage(
        _deps(),
        name="review_code",
        agent=_Reviewer(),
        output_key="staff_reviewer",
        deriver=flag_deriver("review_changes_requested=review_decision:changes_requested"),
    )
    result = await stage.execute(_task())

    assert result.data["review_changes_requested"] is True
    assert result.data["review_decision"] == "changes_requested"  # original verdict still surfaced
