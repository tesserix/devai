"""Dispatch tasks to kagent-managed agents over A2A (Agent2Agent).

Long-lived agents that the kagent agent-sync (workstream G2) has reconciled into
controller-managed Deployments are reachable through the kagent controller's A2A
endpoint at ``{kagent_url}/api/a2a/{namespace}/{agent}``. This client speaks the
standard A2A JSON-RPC ``message/send`` method so devai can hand a task to such an
agent instead of dispatching an ephemeral Job.

It is **additive and opt-in**: the Job runner stays the default execution path.
A caller dispatches here only for agents explicitly marked kagent-managed, and
only when ``settings.kagent_url`` is set. The request shape follows the A2A spec;
confirm the controller's contract in-cluster before enabling it on the hot path.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class KagentError(RuntimeError):
    """Raised when an A2A dispatch to a kagent agent fails."""


class KagentClient:
    def __init__(
        self,
        base_url: str,
        *,
        namespace: str = "kagent",
        timeout_seconds: float = 30.0,
    ) -> None:
        if not base_url:
            raise KagentError("kagent base_url is required")
        self._base = base_url.rstrip("/")
        self._namespace = namespace
        self._timeout = timeout_seconds

    def a2a_url(self, agent: str, namespace: str | None = None) -> str:
        ns = namespace or self._namespace
        return f"{self._base}/api/a2a/{ns}/{agent}"

    @staticmethod
    def _build_request(message: str, message_id: str, request_id: str) -> dict[str, Any]:
        """A2A JSON-RPC 2.0 message/send envelope."""
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "message/send",
            "params": {
                "message": {
                    "role": "user",
                    "parts": [{"kind": "text", "text": message}],
                    "messageId": message_id,
                }
            },
        }

    async def dispatch(
        self,
        agent: str,
        message: str,
        *,
        namespace: str | None = None,
        triggered_by: str = "",
        trace_id: str = "",
        request_id: str = "devai-dispatch",
        message_id: str = "devai-message",
    ) -> dict[str, Any]:
        """Send a task to a kagent agent; return the parsed A2A result.

        Identity is forwarded on the same X-Forwarded-* headers the rest of the
        platform uses, so the kagent agent sees who triggered it. Raises
        KagentError on transport error or a JSON-RPC error response.
        """
        import httpx  # lazy — keeps cold-start light when kagent is unused

        url = self.a2a_url(agent, namespace)
        payload = self._build_request(message, message_id, request_id)
        headers = {"Content-Type": "application/json"}
        if triggered_by:
            headers["X-Forwarded-User"] = triggered_by
        if trace_id:
            headers["X-Trace-Id"] = trace_id

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(url, json=payload, headers=headers)
        except Exception as e:  # noqa: BLE001
            raise KagentError(f"kagent dispatch to {agent!r} failed: {e}") from e

        if resp.status_code >= 400:
            raise KagentError(f"kagent {agent!r} returned {resp.status_code}: {resp.text[:300]}")
        try:
            body = resp.json()
        except Exception as e:  # noqa: BLE001
            raise KagentError(f"kagent {agent!r} returned non-JSON: {resp.text[:200]}") from e
        if isinstance(body, dict) and body.get("error"):
            raise KagentError(f"kagent {agent!r} A2A error: {body['error']}")
        logger.info("kagent: dispatched task to agent %s (ns=%s)", agent, namespace or self._namespace)
        return body.get("result", body) if isinstance(body, dict) else body


def create_kagent_client(settings: Any) -> KagentClient | None:
    """Build a KagentClient from settings, or None when kagent isn't wired.

    Never raises — an unconfigured kagent simply means "dispatch Jobs instead".
    """
    url = getattr(settings, "kagent_url", "") or ""
    if not url:
        return None
    ns = getattr(settings, "kagent_default_namespace", "") or "kagent"
    try:
        return KagentClient(url, namespace=ns)
    except KagentError:
        return None


__all__ = ["KagentClient", "KagentError", "create_kagent_client"]
