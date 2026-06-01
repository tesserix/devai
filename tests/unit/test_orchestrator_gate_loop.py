"""Regression tests for the ALM orchestrator quality-gate loop (Phase-0 fix).

Bug: every gate router (`_route_after_review/security/tests`) reads
``review_iteration`` to decide whether to keep looping, but no node ever
incremented it — so a story that kept getting ``changes_requested`` /
``block`` / failing tests looped through ``implement_story`` forever.

Fix: ``_node_implement_story`` now increments ``review_iteration`` on every
implementation attempt, so the escalating budgets (3 / 3+1 / 3+2) terminate.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from devai.config import Settings
from devai.graph.orchestrator import (
    MAX_REVIEW_ITERATIONS,
    MAX_TEST_FIX_ITERATIONS,
    ALMOrchestrator,
)


@pytest.fixture()
def orch() -> ALMOrchestrator:
    sm = MagicMock()
    sm.redis = MagicMock()
    o = ALMOrchestrator(scm=MagicMock(), state_manager=sm, config=Settings())

    async def _fake_run_node(node, state, path, **kw):  # noqa: ANN001, ANN202
        return {}

    o._run_node = _fake_run_node  # type: ignore[method-assign]
    return o


def test_implement_story_increments_review_iteration(orch: ALMOrchestrator) -> None:
    assert asyncio.run(orch._node_implement_story({"review_iteration": 0}))["review_iteration"] == 1
    assert asyncio.run(orch._node_implement_story({"review_iteration": 5}))["review_iteration"] == 6


def test_review_gate_terminates(orch: ALMOrchestrator) -> None:
    base = {"review_decision": "changes_requested"}
    assert orch._route_after_review({**base, "review_iteration": MAX_REVIEW_ITERATIONS - 1}) == "changes_requested"
    assert orch._route_after_review({**base, "review_iteration": MAX_REVIEW_ITERATIONS}) == "max_iterations"
    assert orch._route_after_review({"review_decision": "approved", "review_iteration": 0}) == "approved"


def test_security_gate_terminates(orch: ALMOrchestrator) -> None:
    blk = {"security_decision": "block"}
    assert orch._route_after_security({**blk, "review_iteration": MAX_REVIEW_ITERATIONS}) == "block"
    assert orch._route_after_security({**blk, "review_iteration": MAX_REVIEW_ITERATIONS + 1}) == "max_blocks"
    assert orch._route_after_security({"security_decision": "pass", "review_iteration": 9}) == "pass"


def test_tests_gate_terminates(orch: ALMOrchestrator) -> None:
    cap = MAX_REVIEW_ITERATIONS + MAX_TEST_FIX_ITERATIONS
    assert orch._route_after_tests({"test_failed": 1, "review_iteration": cap - 1}) == "failed"
    assert orch._route_after_tests({"test_failed": 1, "review_iteration": cap}) == "max_failures"
    assert orch._route_after_tests({"test_failed": 0, "review_iteration": 0}) == "passed"


def test_full_review_loop_cannot_run_forever(orch: ALMOrchestrator) -> None:
    """Simulate a perpetually-rejected story: it must terminate, not loop forever."""
    state: dict = {"review_iteration": 0}
    route = "changes_requested"
    attempts = 0
    while route == "changes_requested" and attempts < 1000:
        patch = asyncio.run(orch._node_implement_story(state))
        state = {**state, **patch}
        route = orch._route_after_review({"review_decision": "changes_requested", **state})
        attempts += 1
    assert route == "max_iterations"
    assert attempts == MAX_REVIEW_ITERATIONS  # 3 attempts, then the gate gives up
