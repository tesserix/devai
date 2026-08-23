"""The Temporal path must honour the same no-replay rule as the executor.

``run_stage_activity`` propagates stage errors so Temporal applies the
workflow's RetryPolicy. For a stage whose outcome is uncertain that retry is
the bug: it re-dispatches work kagent may already be running (ADR-0004).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from temporalio.exceptions import ApplicationError

from devai.agentic.kagent_client import KagentDispatchOutcomeUncertain
from devai.orchestration.activities import run_stage_activity
from devai.pipeline.types import StageResult


class _Stage:
    def __init__(self, exc=None):
        self.exc = exc

    async def execute(self, task):
        if self.exc:
            raise self.exc
        return StageResult(message="done")


@pytest.fixture
def worker_ctx(monkeypatch):
    def _install(stage):
        ctx = SimpleNamespace(
            registry=SimpleNamespace(resolve=lambda key, deps, cfg: stage),
            deps=SimpleNamespace(),
        )
        monkeypatch.setattr("devai.orchestration.activities.get_worker_context", lambda: ctx)

    return _install


TASK = {"id": "t1", "intent": "ship", "repo": "o/r", "blueprint": "bp"}


@pytest.mark.asyncio
async def test_uncertain_outcome_becomes_non_retryable(worker_ctx):
    worker_ctx(_Stage(KagentDispatchOutcomeUncertain("may have been accepted")))
    with pytest.raises(ApplicationError) as err:
        await run_stage_activity("work", "build", {}, TASK)
    assert err.value.non_retryable is True


@pytest.mark.asyncio
async def test_ordinary_failure_stays_retryable(worker_ctx):
    worker_ctx(_Stage(RuntimeError("flaky")))
    with pytest.raises(RuntimeError):
        await run_stage_activity("work", "build", {}, TASK)


@pytest.mark.asyncio
async def test_success_is_unchanged(worker_ctx):
    worker_ctx(_Stage())
    result = await run_stage_activity("work", "build", {}, TASK)
    assert result["message"] == "done"
