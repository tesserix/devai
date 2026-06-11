"""LLMEmbedder — single-text embedding facade over the LLM adapter family.

The pgvector memory adapter wants `embed(text) -> list[float]`; the LLM
adapter family exposes batch `embed(texts, *, model) -> list[list[float]]`.
This shim bridges the two so the memory factory can hand pgvector a real
embedder built from whichever LLM backend supports embeddings, without the
memory family ever importing a vendor SDK.

Failure semantics: raise on any problem (empty result, dimension mismatch,
SDK error). The pgvector adapter's `_maybe_embed()` catches and degrades to
keyword recall — a record with a wrong-sized or garbage vector is worse than
a record with no vector at all.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from devai.adapters.llm.base import LLMAdapter

logger = logging.getLogger(__name__)


class LLMEmbedder:
    """Adapts `LLMAdapter.embed(texts)` to the `embed(text)` surface."""

    def __init__(self, llm: LLMAdapter, *, model: str = "", dimensions: int = 0) -> None:
        self._llm = llm
        self._model = model
        self._dimensions = int(dimensions or 0)

    @property
    def model(self) -> str:
        return self._model

    async def embed(self, text: str) -> list[float]:
        vectors = await self._llm.embed([text], model=self._model)
        if not vectors or not vectors[0]:
            raise ValueError(f"embedder returned no vector (model={self._model})")
        vector = list(vectors[0])
        if self._dimensions and len(vector) != self._dimensions:
            raise ValueError(
                f"embedding dimension mismatch: got {len(vector)}, "
                f"agent_memories column expects {self._dimensions} (model={self._model})"
            )
        return vector

    async def close(self) -> None:
        try:
            await self._llm.close()
        except Exception:  # noqa: BLE001
            logger.debug("embedder close failed", exc_info=True)


__all__ = ["LLMEmbedder"]
