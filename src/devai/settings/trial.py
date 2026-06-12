"""Trial token allowance — try DevAI on the platform keys, then bring your own.

A human user with no LLM connector of their own gets a bounded trial on the
shared platform chain (``DEVAI_LLM_TRIAL_TOKEN_BUDGET`` tokens, 0 disables
trials entirely). Usage accrues permanently in Redis — once the budget is
spent it never resets, so the common keys are auto-revoked for that user
forever and only their own Settings connector can serve them from then on.

The dashboard reads ``GET /api/settings/trial`` to show the depletion banner
(warning ≥80%, hard notice at exhaustion).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from devai.adapters.llm.base import LLMAdapter, LLMRequest, LLMResponse

logger = logging.getLogger(__name__)

_KEY = "devai:llm:trial:{email}"


class TrialMeter:
    """Permanent per-user token counters (Redis; in-memory degradation)."""

    def __init__(self, redis_url: str, budget: int) -> None:
        self._redis_url = redis_url
        self.budget = max(0, int(budget))
        self._redis: Any = None
        self._mem: dict[str, int] = {}  # degraded mode (single-pod accurate)

    async def _client(self) -> Any:
        if self._redis is None and self._redis_url:
            try:
                import redis.asyncio as redis  # noqa: PLC0415

                self._redis = redis.from_url(self._redis_url, decode_responses=True)
            except Exception:  # noqa: BLE001
                logger.warning("trial meter: redis unavailable — in-memory counters", exc_info=True)
                self._redis = False  # sentinel: don't retry every call
        return self._redis or None

    async def used(self, email: str) -> int:
        client = await self._client()
        if client is not None:
            try:
                raw = await client.get(_KEY.format(email=email.lower()))
                return int(raw or 0)
            except Exception:  # noqa: BLE001
                logger.warning("trial meter: read failed — using memory", exc_info=True)
        return self._mem.get(email.lower(), 0)

    async def add(self, email: str, tokens: int) -> int:
        tokens = max(0, int(tokens))
        client = await self._client()
        if client is not None:
            try:
                # No TTL on purpose: exhaustion is permanent (auto-revoke).
                return int(await client.incrby(_KEY.format(email=email.lower()), tokens))
            except Exception:  # noqa: BLE001
                logger.warning("trial meter: incr failed — using memory", exc_info=True)
        self._mem[email.lower()] = self._mem.get(email.lower(), 0) + tokens
        return self._mem[email.lower()]

    async def remaining(self, email: str) -> int:
        return max(0, self.budget - await self.used(email))

    async def exhausted(self, email: str) -> bool:
        return self.budget <= 0 or await self.used(email) >= self.budget

    async def status(self, email: str) -> dict[str, Any]:
        used = await self.used(email)
        return {
            "trial_enabled": self.budget > 0,
            "budget": self.budget,
            "used": used,
            "remaining": max(0, self.budget - used),
            "exhausted": self.budget <= 0 or used >= self.budget,
            "warning": self.budget > 0 and used >= int(self.budget * 0.8),
        }


_METER: TrialMeter | None = None


def get_trial_meter(settings: Any) -> TrialMeter:
    """Process-wide meter (counters live in Redis, so sharing one is safe)."""
    global _METER  # noqa: PLW0603
    budget = int(getattr(settings, "llm_trial_token_budget", 0) or 0)
    if _METER is None or _METER.budget != budget:
        _METER = TrialMeter(getattr(settings, "redis_url", "") or "", budget)
    return _METER


class TrialLLMAdapter(LLMAdapter):
    """Platform chain, metered against one user's permanent trial budget."""

    def __init__(self, inner: LLMAdapter, meter: TrialMeter, email: str) -> None:
        self._inner = inner
        self._meter = meter
        self._email = email

    @property
    def provider_name(self) -> str:  # type: ignore[override]
        return f"trial({self._inner.provider_name})"

    @property
    def default_model(self) -> str:  # type: ignore[override]
        return self._inner.default_model

    @staticmethod
    def _exhausted_response() -> LLMResponse:
        return LLMResponse(
            text=(
                "Your free trial allowance is used up. Add your own LLM API key "
                "in Settings → LLM Provider to continue — the shared platform "
                "credentials are no longer available for your account."
            ),
            finish_reason="error",
            provider="trial",
            extra={"trial_exhausted": True},
        )

    async def generate(self, request: LLMRequest) -> LLMResponse:
        if await self._meter.exhausted(self._email):
            return self._exhausted_response()
        resp = await self._inner.generate(request)
        total = await self._meter.add(self._email, resp.usage.total_tokens or 0)
        resp.extra["trial"] = True
        resp.extra["trial_remaining"] = max(0, self._meter.budget - total)
        return resp

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMResponse]:
        if await self._meter.exhausted(self._email):
            yield self._exhausted_response()
            return
        last_usage = 0
        async for chunk in self._inner.stream(request):
            last_usage = max(last_usage, chunk.usage.total_tokens or 0)
            yield chunk
        await self._meter.add(self._email, last_usage)

    async def embed(self, texts: list[str], *, model: str = "") -> list[list[float]]:
        if await self._meter.exhausted(self._email):
            raise RuntimeError("trial allowance exhausted — configure your own LLM connector")
        return await self._inner.embed(texts, model=model)

    async def list_models(self) -> list[dict[str, str]]:
        return await self._inner.list_models()

    async def health_check(self) -> dict[str, Any]:
        return await self._inner.health_check()

    async def close(self) -> None:
        # The inner adapter is the shared platform chain — never close it here.
        return None


__all__ = ["TrialLLMAdapter", "TrialMeter", "get_trial_meter"]
