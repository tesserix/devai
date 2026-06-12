"""Fallback chain + model allowlist — no workflow fails on a provider outage."""

from __future__ import annotations

import pytest

from devai.adapters.llm.base import LLMAdapter, LLMRequest, LLMResponse
from devai.adapters.llm.fallback import FallbackLLMAdapter, ModelAllowlistLLMAdapter


class _Boom(LLMAdapter):
    provider_name = "boom"
    default_model = "boom-1"

    async def generate(self, request: LLMRequest) -> LLMResponse:
        raise RuntimeError("provider outage")


class _ErrorResponse(LLMAdapter):
    provider_name = "erroring"
    default_model = "err-1"

    async def generate(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(text="", finish_reason="error", provider=self.provider_name)


class _Echo(LLMAdapter):
    provider_name = "echo"
    default_model = "echo-1"

    def __init__(self) -> None:
        self.seen: list[LLMRequest] = []

    async def generate(self, request: LLMRequest) -> LLMResponse:
        self.seen.append(request)
        return LLMResponse(
            text="ok", finish_reason="stop", provider=self.provider_name, model=request.model or self.default_model
        )


@pytest.mark.asyncio
async def test_fallback_on_exception_tags_and_clears_foreign_model():
    echo = _Echo()
    chain = FallbackLLMAdapter(_Boom(), echo)
    resp = await chain.generate(LLMRequest(model="boom-only-model"))
    assert resp.text == "ok"
    assert resp.extra.get("fallback") is True
    assert resp.extra.get("fallback_from") == "boom"
    # The primary's model id means nothing to the fallback → cleared.
    assert echo.seen[0].model == ""


@pytest.mark.asyncio
async def test_fallback_on_error_response():
    chain = FallbackLLMAdapter(_ErrorResponse(), _Echo())
    resp = await chain.generate(LLMRequest())
    assert resp.text == "ok" and resp.extra.get("fallback") is True


@pytest.mark.asyncio
async def test_all_failed_returns_error_response_not_exception():
    chain = FallbackLLMAdapter(_Boom(), _ErrorResponse())
    resp = await chain.generate(LLMRequest())
    assert resp.finish_reason == "error"  # stage-level recovery handles it


def test_chain_builder_without_fallback_returns_primary():
    from devai.adapters.llm.factory import create_llm_chain

    class _S:
        llm_provider = "noop"
        llm_noop_canned_text = "[noop]"
        llm_fallback_provider = ""

    assert create_llm_chain(_S()).provider_name == "noop"


def test_chain_builder_skips_unconfigured_fallback():
    from devai.adapters.llm.factory import create_llm_chain

    class _S:
        llm_provider = "noop"
        llm_noop_canned_text = "[noop]"
        llm_fallback_provider = "groq"  # no key → noop → chain disabled

    assert create_llm_chain(_S()).provider_name == "noop"


@pytest.mark.asyncio
async def test_allowlist_clamps_disabled_models_and_filters_listing():
    echo = _Echo()
    guarded = ModelAllowlistLLMAdapter(echo, ["echo-1", "echo-2"])
    # Allowed model passes through untouched.
    await guarded.generate(LLMRequest(model="echo-2"))
    assert echo.seen[-1].model == "echo-2"
    # Disabled model clamps to the provider default (empty → default).
    await guarded.generate(LLMRequest(model="forbidden-model"))
    assert echo.seen[-1].model == ""
    assert guarded.provider_name == "echo"


def test_chain_builder_orders_and_skips_unconfigured_links():
    from devai.adapters.llm.factory import create_llm_chain

    class _S:
        # Primary: vertex (api-key mode → buildable without google-auth)
        llm_provider = "vertex_gemini"
        vertex_project = "proj-1"
        vertex_api_key = "AQ.test"
        llm_noop_canned_text = "[noop]"
        # groq configured; openrouter NOT (skipped); anthropic configured.
        llm_fallback_provider = "groq,openrouter,anthropic"
        groq_api_key = "gsk_test"
        anthropic_api_key = "sk-ant-test"

    chain = create_llm_chain(_S())
    assert chain.provider_name == "vertex_gemini→groq→anthropic"


@pytest.mark.asyncio
async def test_model_fallback_retries_other_model_on_error():
    from devai.adapters.llm.base import LLMUsage
    from devai.adapters.llm.fallback import ModelFallbackLLMAdapter

    class _ModelPicky(LLMAdapter):
        provider_name = "picky"
        default_model = "primary"

        async def generate(self, request: LLMRequest) -> LLMResponse:
            if (request.model or "primary") == "primary":
                return LLMResponse(text="", finish_reason="error", provider="picky")
            return LLMResponse(text="ok-on-fallback", finish_reason="stop",
                               provider="picky", usage=LLMUsage())

    a = ModelFallbackLLMAdapter(_ModelPicky(), "backup-model")
    resp = await a.generate(LLMRequest(model="primary"))
    assert resp.text == "ok-on-fallback"
    assert resp.extra.get("model_fallback") is True
