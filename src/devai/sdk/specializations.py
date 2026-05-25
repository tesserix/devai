"""Specializations sub-client for the DevAI SDK.

Mirrors :class:`devai.sdk.pipeline.PipelineSDK` but for
``/api/specializations``. Specializations are the role personas
(coder / staff_reviewer / requirements_engineer / …) that pipeline
stages bind to. The SDK reads them out so embedding apps can preview
which roles are available and what tool budgets they get.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class SpecializationsSDK:
    def __init__(self, *, base_url: str, timeout_seconds: float = 5.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds

    def list(self, *, category: str = "") -> list[dict[str, Any]]:
        params: dict[str, str] = {}
        if category:
            params["category"] = category
        return self._get("/api/specializations", params=params)

    def by_category(self) -> dict[str, list[dict[str, Any]]]:
        return self._get("/api/specializations/by-category")

    def _get(self, path: str, *, params: dict[str, str] | None = None) -> Any:
        from devai.sdk._http import lazy_httpx
        from devai.sdk.client import DevAIError

        httpx = lazy_httpx()
        url = f"{self._base_url}{path}"
        try:
            r = httpx.request(
                "GET",
                url,
                params=params,
                timeout=self._timeout,
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as e:  # type: ignore[union-attr]
            raise DevAIError(f"specializations GET {path}: {e}") from e
        if r.status_code >= 400:
            raise DevAIError(f"specializations GET {path} → {r.status_code}: {r.text[:200]}")
        return r.json() if r.text else None
