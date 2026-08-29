"""Dispatch tasks to kagent-managed runtimes over A2A (Agent2Agent).

The kagent controller exposes classic Agents at ``/api/a2a/{namespace}/{agent}``
and Substrate-backed SandboxAgents at
``/api/a2a-sandboxes/{namespace}/{agent}``. This client speaks the standard A2A
JSON-RPC ``message/send`` method so DevAI can hand a task to either target instead
of dispatching an ephemeral Job.

It is **additive and opt-in**: the Job runner stays the default execution path.
A caller dispatches here only for agents explicitly marked kagent-managed, and
only when ``settings.kagent_url`` is set. The request shape follows the A2A spec;
confirm the controller's contract in-cluster before enabling it on the hot path.
"""

from __future__ import annotations

import logging
import re
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

# The registry label that marks an agent as a kagent-managed Substrate Actor
# rather than a per-run Job. The
# kagent-agent-sync reconciler selects on the same label, so the reconciler
# and the dispatcher agree on which agents kagent owns.
RUNTIME_LABEL = "devai.io/runtime"
RUNTIME_KAGENT = "kagent"

# Kubernetes object names (agents, namespaces) are RFC-1123 DNS labels:
# lowercase alphanumerics and '-', starting/ending alphanumeric. Validating
# before interpolation prevents path traversal / SSRF via crafted segments
# (e.g. ``../`` or ``foo/bar``) in the A2A URL.
_DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
_TRUSTED_CONTROLLER_HOSTS = {
    "kagent-controller.kagent-system.svc",
    "kagent-controller.kagent-system.svc.cluster.local",
}


def _valid_k8s_name(value: str) -> bool:
    return bool(value) and bool(_DNS_LABEL.match(value))


def _validate_controller_url(url: str) -> bool:
    parts = urlsplit(url)
    if parts.username or parts.password:
        raise KagentError("kagent controller URL must not contain credentials")
    host = (parts.hostname or "").lower()
    if host in _TRUSTED_CONTROLLER_HOSTS and parts.scheme in ("http", "https"):
        return True

    from devai.tools.url_guard import UnsafeURLError, assert_public_url

    try:
        assert_public_url(url)
    except UnsafeURLError as e:
        raise KagentError("kagent controller URL was rejected by the outbound request guard") from e
    return False


class KagentError(RuntimeError):
    """Raised when an A2A dispatch to a kagent agent fails."""


class KagentDispatchOutcomeUncertain(KagentError):
    """The dispatch may have been accepted, so automatic replay is unsafe.

    ``retry_unsafe`` is the contract the pipeline executor reads: a stage that
    fails with it must not be re-run, because the first attempt may already be
    executing side-effecting tools.
    """

    retry_unsafe = True


class KagentDispatchTarget(StrEnum):
    AGENT = "agent"
    SANDBOX_AGENT = "sandbox_agent"


class KagentClient:
    def __init__(
        self,
        base_url: str,
        *,
        namespace: str = "kagent",
        timeout_seconds: float = 30.0,
        service_token: str = "",
    ) -> None:
        if not base_url:
            raise KagentError("kagent base_url is required")
        self._base = base_url.rstrip("/")
        if namespace and not _valid_k8s_name(namespace):
            raise KagentError(f"invalid kagent namespace {namespace!r} (must be a DNS-1123 label)")
        self._namespace = namespace
        self._timeout = timeout_seconds
        # The auth-bff shared secret. Stamped as X-Auth-Bff-Secret so the
        # controller's identity.forward_trusted gate accepts our forwarded
        # identity instead of silently dropping it (see CODE-5/CODE-17).
        self._service_token = service_token

    def a2a_url(
        self,
        agent: str,
        namespace: str | None = None,
        *,
        target: KagentDispatchTarget = KagentDispatchTarget.AGENT,
    ) -> str:
        ns = namespace or self._namespace
        if not _valid_k8s_name(agent):
            raise KagentError(f"invalid kagent agent name {agent!r} (must be a DNS-1123 label)")
        if not _valid_k8s_name(ns):
            raise KagentError(f"invalid kagent namespace {ns!r} (must be a DNS-1123 label)")
        route = "a2a-sandboxes" if target is KagentDispatchTarget.SANDBOX_AGENT else "a2a"
        return f"{self._base}/api/{route}/{ns}/{agent}"

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
        target: KagentDispatchTarget = KagentDispatchTarget.AGENT,
        triggered_by: str = "",
        trace_id: str = "",
        api_key: str = "",
        request_id: str,
        message_id: str,
    ) -> dict[str, Any]:
        """Send a task to a kagent runtime; return the parsed A2A result.

        Identity is forwarded on the same X-Forwarded-* headers the rest of the
        platform uses, so the kagent agent sees who triggered it. When
        ``api_key`` is set it rides as the ``Authorization: Bearer`` token: a
        kagent ModelConfig with ``apiKeyPassthrough`` forwards that token to the
        LLM provider as the API key, so a shared agent runs on the triggering
        user's OWN key (no per-user secret in kagent). Callers must provide
        stable per-invocation IDs so an accepted dispatch can be correlated
        without replaying it under a new identity.
        """
        import httpx  # lazy — keeps cold-start light when kagent is unused

        url = self.a2a_url(agent, namespace, target=target)
        payload = self._build_request(message, message_id, request_id)
        headers = {"Content-Type": "application/json"}
        # Stamp the shared secret so the forwarded identity is actually
        # trusted downstream. Without it, X-Forwarded-User is dropped by the
        # receiver's forward-trusted gate (it is meaningless on its own).
        if self._service_token:
            headers["X-Auth-Bff-Secret"] = self._service_token
        if triggered_by:
            headers["X-Forwarded-User"] = triggered_by
        if trace_id:
            headers["X-Trace-Id"] = trace_id
        # Per-user LLM key for apiKeyPassthrough ModelConfigs. Never logged.
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(url, json=payload, headers=headers)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as e:
            raise KagentError(f"kagent dispatch to {agent!r} was rejected before connection") from e
        except Exception as e:  # noqa: BLE001 - unknown transport state must fail closed
            raise KagentDispatchOutcomeUncertain(
                f"kagent dispatch to {agent!r} may have been accepted; automatic fallback is unsafe"
            ) from e

        if resp.status_code >= 500:
            raise KagentDispatchOutcomeUncertain(
                f"kagent dispatch to {agent!r} returned HTTP {resp.status_code} after possible acceptance"
            )
        if resp.status_code >= 300:
            raise KagentError(f"kagent dispatch to {agent!r} was rejected with HTTP {resp.status_code}")
        try:
            body = resp.json()
        except Exception as e:  # noqa: BLE001
            raise KagentDispatchOutcomeUncertain(
                f"kagent dispatch to {agent!r} returned an invalid response after possible acceptance"
            ) from e
        if isinstance(body, dict) and body.get("error"):
            raise KagentDispatchOutcomeUncertain(
                f"kagent dispatch to {agent!r} returned an A2A error after possible acceptance"
            )
        logger.info("kagent: dispatched task to agent %s (ns=%s)", agent, namespace or self._namespace)
        result = body.get("result", body) if isinstance(body, dict) else body
        if not isinstance(result, dict):
            raise KagentDispatchOutcomeUncertain(
                f"kagent dispatch to {agent!r} returned an invalid result after possible acceptance"
            )
        return result


def extract_a2a_text(result: Any) -> str:
    """Pull the agent's reply text out of an A2A ``message/send`` result.

    A2A ``message/send`` returns either a Message (``{role, parts:[...]}``) or a
    Task (``{status:{message:{parts}}, artifacts:[{parts}], history:[...]}``).
    We look in the most-specific places first and concatenate every text part
    we find, so the dispatcher gets the agent's output regardless of which
    shape the controller returns. Returns "" when there is no text to surface
    (the caller still keeps the raw result for the dashboard).
    """

    def _parts_text(parts: Any) -> list[str]:
        out: list[str] = []
        if isinstance(parts, list):
            for p in parts:
                if isinstance(p, dict) and p.get("kind") == "text" and isinstance(p.get("text"), str):
                    out.append(p["text"])
        return out

    if not isinstance(result, dict):
        return str(result) if result else ""

    chunks: list[str] = []
    # 1. Task.status.message.parts — the agent's final message.
    status = result.get("status")
    if isinstance(status, dict) and isinstance(status.get("message"), dict):
        chunks += _parts_text(status["message"].get("parts"))
    # 2. Task.artifacts[].parts — produced artifacts.
    artifacts = result.get("artifacts")
    if isinstance(artifacts, list):
        for art in artifacts:
            if isinstance(art, dict):
                chunks += _parts_text(art.get("parts"))
    # 3. A bare Message result (no Task wrapper).
    if not chunks:
        chunks += _parts_text(result.get("parts"))
    return "\n".join(c for c in chunks if c).strip()


def create_kagent_client(settings: Any) -> KagentClient | None:
    """Build a KagentClient from settings, or None when kagent isn't wired.

    Never raises — an unconfigured kagent simply means "dispatch Jobs instead".
    """
    url = getattr(settings, "kagent_url", "") or ""
    if not url:
        return None
    ns = getattr(settings, "kagent_default_namespace", "") or "kagent"
    try:
        trusted_controller = _validate_controller_url(url)
        service_token = (getattr(settings, "auth_bff_shared_secret", "") or "") if trusted_controller else ""
        return KagentClient(url, namespace=ns, service_token=service_token)
    except KagentError:
        logger.warning("kagent client configuration rejected")
        return None


__all__ = [
    "RUNTIME_KAGENT",
    "RUNTIME_LABEL",
    "KagentClient",
    "KagentDispatchOutcomeUncertain",
    "KagentDispatchTarget",
    "KagentError",
    "create_kagent_client",
    "extract_a2a_text",
]
