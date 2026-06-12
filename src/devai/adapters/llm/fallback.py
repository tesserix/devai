"""Resilience wrappers — fallback chains and per-user model allowlists.

``FallbackLLMAdapter`` makes provider outages non-fatal: a failed call
(exception or an ``finish_reason == "error"`` response) moves to the next
adapter in the chain instead of failing the agent/stage. When a fallback
serves the request its response is tagged (``extra["fallback_from"]``) so
the instrumented metrics show the fallback rate honestly.

``ModelAllowlistLLMAdapter`` enforces a user's enabled-models choice from
Settings: a request for a disabled model is clamped to the adapter's
default model rather than rejected — agents keep working, the user's
policy still holds.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

from devai.adapters.llm.base import LLMAdapter, LLMRequest, LLMResponse

logger = logging.getLogger(__name__)


class FallbackLLMAdapter(LLMAdapter):
    """Try each adapter in order; the first healthy answer wins.

    ``preserve_model=True`` keeps the request's model id down the chain —
    used by role routes where the fallback provider serves the SAME models
    (the gateway routes Claude on Vertex, so `claude-fable-5` is valid on
    both links). Default behavior clears the model so heterogeneous
    fallbacks use their own defaults.
    """

    def __init__(self, primary: LLMAdapter, *fallbacks: LLMAdapter, preserve_model: bool = False) -> None:
        self._chain: list[LLMAdapter] = [primary, *[f for f in fallbacks if f is not None]]
        self._preserve_model = preserve_model

    @property
    def provider_name(self) -> str:  # type: ignore[override]
        names = "→".join(a.provider_name for a in self._chain)
        return names if len(self._chain) > 1 else self._chain[0].provider_name

    @property
    def default_model(self) -> str:  # type: ignore[override]
        return self._chain[0].default_model

    @staticmethod
    def _failed(response: LLMResponse) -> bool:
        return response.finish_reason == "error"

    def _request_for(self, adapter: LLMAdapter, request: LLMRequest, *, is_primary: bool) -> LLMRequest:
        """Fallback providers can't serve the primary's model id — clear it
        so the fallback's own default model applies."""
        if is_primary or not request.model or self._preserve_model:
            return request
        return replace(request, model="")

    async def generate(self, request: LLMRequest) -> LLMResponse:
        last: LLMResponse | None = None
        for i, adapter in enumerate(self._chain):
            try:
                resp = await adapter.generate(self._request_for(adapter, request, is_primary=i == 0))
            except Exception:  # noqa: BLE001
                logger.warning(
                    "llm fallback: %s generate failed (%d/%d in chain)",
                    adapter.provider_name,
                    i + 1,
                    len(self._chain),
                    exc_info=True,
                )
                continue
            if self._failed(resp) and i + 1 < len(self._chain):
                logger.warning(
                    "llm fallback: %s answered with an error response — trying next provider",
                    adapter.provider_name,
                )
                last = resp
                continue
            if i > 0:
                resp.extra["fallback_from"] = self._chain[0].provider_name
                resp.extra["fallback"] = True
            return resp
        return last or LLMResponse(
            text="",
            finish_reason="error",
            provider=self.provider_name,
            extra={"error": "all providers in the fallback chain failed"},
        )

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMResponse]:
        """Stream from the primary; if it fails before/while streaming, fall
        back to a one-shot generate on the rest of the chain."""
        try:
            async for chunk in self._chain[0].stream(request):
                yield chunk
            return
        except Exception:  # noqa: BLE001
            logger.warning("llm fallback: primary stream failed — degrading to generate", exc_info=True)
        if len(self._chain) > 1:
            yield await FallbackLLMAdapter(*self._chain[1:]).generate(
                self._request_for(self._chain[1], request, is_primary=False)
            )

    async def embed(self, texts: list[str], *, model: str = "") -> list[list[float]]:
        last_exc: Exception | None = None
        for adapter in self._chain:
            try:
                return await adapter.embed(texts, model=model if adapter is self._chain[0] else "")
            except Exception as e:  # noqa: BLE001
                last_exc = e
        raise last_exc or NotImplementedError("no adapter in the chain implements embeddings")

    async def list_models(self) -> list[dict[str, str]]:
        return await self._chain[0].list_models()

    async def health_check(self) -> dict[str, Any]:
        checks = []
        for adapter in self._chain:
            try:
                checks.append(await adapter.health_check())
            except Exception as e:  # noqa: BLE001
                checks.append({"ok": False, "provider": adapter.provider_name, "detail": str(e)})
        return {"ok": any(c.get("ok") for c in checks), "provider": self.provider_name, "chain": checks}

    async def close(self) -> None:
        for adapter in self._chain:
            try:
                await adapter.close()
            except Exception:  # noqa: BLE001
                pass


class ModelAllowlistLLMAdapter(LLMAdapter):
    """Clamp requests to the models a user enabled in Settings."""

    def __init__(self, inner: LLMAdapter, allowed: list[str]) -> None:
        self._inner = inner
        self._allowed = {m.strip() for m in allowed if m and m.strip()}

    @property
    def provider_name(self) -> str:  # type: ignore[override]
        return self._inner.provider_name

    @property
    def default_model(self) -> str:  # type: ignore[override]
        return self._inner.default_model

    def _clamp(self, request: LLMRequest) -> LLMRequest:
        model = request.model or self._inner.default_model
        if self._allowed and model and model not in self._allowed:
            logger.info(
                "llm allowlist: model %r is disabled for this user — using %r",
                model,
                self._inner.default_model or "(provider default)",
            )
            return replace(request, model="")
        return request

    async def generate(self, request: LLMRequest) -> LLMResponse:
        return await self._inner.generate(self._clamp(request))

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMResponse]:
        async for chunk in self._inner.stream(self._clamp(request)):
            yield chunk

    async def embed(self, texts: list[str], *, model: str = "") -> list[list[float]]:
        return await self._inner.embed(texts, model=model)

    async def list_models(self) -> list[dict[str, str]]:
        models = await self._inner.list_models()
        if not self._allowed:
            return models
        return [m for m in models if m.get("id") in self._allowed]

    async def health_check(self) -> dict[str, Any]:
        return await self._inner.health_check()

    async def close(self) -> None:
        await self._inner.close()


class ModelFallbackLLMAdapter(LLMAdapter):
    """Retry on a second MODEL of the SAME provider before giving up.

    A user picks a primary + fallback model in Settings; when the primary
    model errors (e.g. that model is overloaded), the call retries on the
    fallback model through the same adapter/credentials before the outer
    provider chain takes over.
    """

    def __init__(self, inner: LLMAdapter, fallback_model: str) -> None:
        self._inner = inner
        self._fallback_model = fallback_model

    @property
    def provider_name(self) -> str:  # type: ignore[override]
        return self._inner.provider_name

    @property
    def default_model(self) -> str:  # type: ignore[override]
        return self._inner.default_model

    async def generate(self, request: LLMRequest) -> LLMResponse:
        try:
            resp = await self._inner.generate(request)
        except Exception:  # noqa: BLE001
            logger.warning("model fallback: primary errored — retrying on %s", self._fallback_model, exc_info=True)
            return await self._inner.generate(replace(request, model=self._fallback_model))
        if resp.finish_reason == "error" and (request.model or "") != self._fallback_model:
            logger.warning("model fallback: primary returned error — retrying on %s", self._fallback_model)
            alt = await self._inner.generate(replace(request, model=self._fallback_model))
            alt.extra["model_fallback"] = True
            return alt
        return resp

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMResponse]:
        async for chunk in self._inner.stream(request):
            yield chunk

    async def embed(self, texts: list[str], *, model: str = "") -> list[list[float]]:
        return await self._inner.embed(texts, model=model)

    async def list_models(self) -> list[dict[str, str]]:
        return await self._inner.list_models()

    async def health_check(self) -> dict[str, Any]:
        return await self._inner.health_check()

    async def close(self) -> None:
        await self._inner.close()


__all__ = ["FallbackLLMAdapter", "ModelAllowlistLLMAdapter", "ModelFallbackLLMAdapter"]
