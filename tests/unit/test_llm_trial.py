"""Trial allowance governance — works while budget lasts, hard-revoked after."""

from __future__ import annotations

import pytest

from devai.adapters.llm.base import LLMAdapter, LLMRequest, LLMResponse, LLMUsage
from devai.settings.trial import TrialLLMAdapter, TrialMeter


class _Echo(LLMAdapter):
    provider_name = "platform"
    default_model = "m1"

    async def generate(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(text="ok", finish_reason="stop", usage=LLMUsage(total_tokens=400))


def _meter(budget: int) -> TrialMeter:
    return TrialMeter(redis_url="", budget=budget)  # in-memory mode for tests


@pytest.mark.asyncio
async def test_trial_meters_and_then_revokes_permanently():
    meter = _meter(1000)
    adapter = TrialLLMAdapter(_Echo(), meter, "new.user@example.com")

    # Works during the trial, usage accrues, remaining is reported.
    r1 = await adapter.generate(LLMRequest())
    assert r1.text == "ok" and r1.extra["trial"] is True
    assert r1.extra["trial_remaining"] == 600

    r2 = await adapter.generate(LLMRequest())
    assert r2.extra["trial_remaining"] == 200

    r3 = await adapter.generate(LLMRequest())  # crosses the budget (1200 ≥ 1000)
    assert r3.text == "ok"

    # NOW EXHAUSTED — the platform chain must refuse, permanently.
    r4 = await adapter.generate(LLMRequest())
    assert r4.finish_reason == "error"
    assert r4.extra.get("trial_exhausted") is True
    assert "Settings" in r4.text  # tells the user exactly what to do
    # Still refused on every later attempt (no reset).
    r5 = await adapter.generate(LLMRequest())
    assert r5.extra.get("trial_exhausted") is True


@pytest.mark.asyncio
async def test_trial_isolated_per_user():
    meter = _meter(500)
    a = TrialLLMAdapter(_Echo(), meter, "a@example.com")
    b = TrialLLMAdapter(_Echo(), meter, "b@example.com")
    await a.generate(LLMRequest())
    await a.generate(LLMRequest())  # a: 800 ≥ 500 → exhausted
    assert (await a.generate(LLMRequest())).extra.get("trial_exhausted") is True
    # b is untouched by a's exhaustion.
    assert (await b.generate(LLMRequest())).text == "ok"


@pytest.mark.asyncio
async def test_strict_mode_with_budget_returns_trial_adapter():
    from devai.pipeline.interfaces import StageDeps

    class _Cfg:
        llm_require_user_connector = True
        llm_trial_token_budget = 1000
        redis_url = ""

    class _NoneResolver:
        async def resolve_for_email(self, email):
            return None

    deps = StageDeps(config=_Cfg(), llm=_Echo(), llm_resolver=_NoneResolver())
    adapter = await deps.llm_for_principal("new.user@example.com")
    assert adapter is not None and adapter.provider_name == "trial(platform)"

    # Budget 0 → strict block (None), exactly as before trials existed.
    class _CfgNoTrial(_Cfg):
        llm_trial_token_budget = 0

    deps2 = StageDeps(config=_CfgNoTrial(), llm=_Echo(), llm_resolver=_NoneResolver())
    assert await deps2.llm_for_principal("new.user@example.com") is None


@pytest.mark.asyncio
async def test_meter_status_shape_for_banner():
    meter = _meter(1000)
    await meter.add("u@example.com", 850)
    s = await meter.status("u@example.com")
    assert s["warning"] is True and s["exhausted"] is False and s["remaining"] == 150
    await meter.add("u@example.com", 200)
    s = await meter.status("u@example.com")
    assert s["exhausted"] is True and s["remaining"] == 0
