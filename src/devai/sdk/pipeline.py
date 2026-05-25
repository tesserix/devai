"""Pipeline sub-client for the DevAI SDK.

Talks to the FastAPI endpoints under ``/api/pipeline`` exposed by the
webhook app. The SDK never reaches into pipeline internals — it goes
through the same REST surface a third-party caller would. That keeps
the SDK a stable contract independent of how the runtime evolves.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Iterator

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class AgentRun:
    """Handle to a pipeline run. Returned from
    :meth:`PipelineSDK.dispatch`. ``id`` is the run identifier the API
    assigned; everything else is a snapshot of the launch response."""

    id: str
    blueprint: str
    state: str
    raw: dict[str, Any]


class PipelineSDK:
    """Thin facade over ``/api/pipeline``.

    Mirrors :class:`devai.registry.RegistryClient`'s discipline: sync
    methods, lazy ``httpx`` import, raises :class:`DevAIError` on
    HTTP-level failures.
    """

    def __init__(self, *, base_url: str, timeout_seconds: float = 5.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds

    # ---- public surface --------------------------------------------------

    def list_blueprints(self) -> list[dict[str, Any]]:
        return self._get("/api/pipeline/blueprints")

    def list_runs(self, limit: int = 50, blueprint: str = "") -> list[dict[str, Any]]:
        params: dict[str, str] = {"limit": str(limit)}
        if blueprint:
            params["blueprint"] = blueprint
        return self._get("/api/pipeline/runs", params=params)

    def get_run(self, run_id: str) -> dict[str, Any]:
        return self._get(f"/api/pipeline/runs/{run_id}")

    def dispatch(
        self,
        *,
        blueprint: str,
        intent: str,
        repo: str = "",
        config: dict[str, Any] | None = None,
    ) -> AgentRun:
        body = {
            "blueprint": blueprint,
            "intent": intent,
            "repo": repo,
            "config": config or {},
        }
        raw = self._post("/api/pipeline/runs", body=body)
        return AgentRun(
            id=raw.get("id", ""),
            blueprint=raw.get("blueprint", blueprint),
            state=raw.get("state", "pending"),
            raw=raw,
        )

    def cancel(self, run_id: str) -> dict[str, Any]:
        return self._post(f"/api/pipeline/runs/{run_id}/cancel", body={})

    def stream(self, run_id: str) -> Iterator[dict[str, Any]]:
        """Yield SSE events for a run. Blocks until the server closes
        the stream (i.e. the run reaches a terminal state)."""
        from devai.sdk._http import lazy_httpx  # local import keeps deps lazy

        httpx = lazy_httpx()
        url = f"{self._base_url}/api/pipeline/runs/{run_id}/events"
        try:
            with httpx.stream("GET", url, timeout=None, headers={"Accept": "text/event-stream"}) as r:
                r.raise_for_status()
                for line in r.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[len("data:") :].strip()
                    if not payload:
                        continue
                    try:
                        yield json.loads(payload)
                    except json.JSONDecodeError:
                        # Heartbeat lines (e.g. ":ping") fall in here;
                        # drop silently rather than crash the iterator.
                        continue
        except httpx.HTTPError as e:  # type: ignore[union-attr]
            from devai.sdk.client import DevAIError

            raise DevAIError(f"pipeline.stream({run_id}): {e}") from e

    # ---- HTTP plumbing ---------------------------------------------------

    def _get(self, path: str, *, params: dict[str, str] | None = None) -> Any:
        return self._request("GET", path, params=params, body=None)

    def _post(self, path: str, *, body: dict[str, Any]) -> Any:
        return self._request("POST", path, params=None, body=body)

    def _request(self, method: str, path: str, *, params, body) -> Any:
        from devai.sdk._http import lazy_httpx
        from devai.sdk.client import DevAIError

        httpx = lazy_httpx()
        url = f"{self._base_url}{path}"
        try:
            r = httpx.request(
                method,
                url,
                params=params,
                json=body,
                timeout=self._timeout,
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as e:  # type: ignore[union-attr]
            raise DevAIError(f"pipeline {method} {path}: {e}") from e

        if r.status_code >= 400:
            raise DevAIError(f"pipeline {method} {path} → {r.status_code}: {r.text[:200]}")
        if r.text and r.headers.get("content-type", "").startswith("application/json"):
            return r.json()
        return None
