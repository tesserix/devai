"""A2A runtime client — discover an agent via the registry, fetch its Agent
Card, and invoke it over the A2A protocol through the approved AI Gateway.

The signed card contributes only its A2A path. Its host is never dialed: the
gateway origin is configured by DevAI and remains the outbound trust boundary.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import socket
import uuid
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from devai.a2a.card import AgentCard
from devai.a2a.errors import A2AError
from devai.a2a.verify import check_service_url, verify_card_signature

if TYPE_CHECKING:
    from devai.config import Settings
    from devai.registry.client import RegistryClient

logger = logging.getLogger(__name__)

# In-cluster service DNS — the safe default the SSRF allowlist permits.
_DEFAULT_URL_SUFFIXES = [".svc.cluster.local", ".svc"]


def resolve_host_block_check(url: str, in_cluster_suffixes: list[str]) -> None:
    """Resolve a card URL's hostname and reject non-routable/metadata targets.

    ``check_service_url`` only blocks IP *literals*; a hostname that resolves to
    a loopback / link-local (169.254.169.254) / unspecified / reserved address
    sails past the suffix allowlist. We resolve the host and fail closed if any
    resolved address is dangerous. Unresolvable hosts are left to the transport
    layer (the call will simply fail). Raises :class:`A2AError` on rejection.

    In-cluster Service DNS legitimately resolves to RFC-1918 ClusterIPs, so the
    broad ``is_private`` block is applied only to hosts OUTSIDE the in-cluster
    suffix allowlist; loopback/link-local/metadata/unspecified/reserved are
    always blocked (those are never a valid Service ClusterIP).
    """
    host = urlparse(url).hostname
    if not host:
        return
    # Literals are already handled by check_service_url; skip resolution.
    try:
        ipaddress.ip_address(host)
        return
    except ValueError:
        pass
    host_l = host.lower()
    is_in_cluster = any(
        host_l == s.lower().lstrip(".") or host_l.endswith(s.lower()) for s in in_cluster_suffixes if s != "*"
    )
    try:
        infos = socket.getaddrinfo(host_l, None)
    except OSError:
        return
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        dangerous = ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_reserved or ip.is_multicast
        if not is_in_cluster:
            # External targets must not resolve into RFC-1918 space either.
            dangerous = dangerous or ip.is_private
        if dangerous:
            raise A2AError(f"a2a: refusing to call {host!r} — resolves to non-routable {addr} (SSRF guard)")


class A2AClient:
    """Thin, dependency-light A2A consumer for the orchestrator.

    Holds a reference to a :class:`RegistryClient` for discovery/card fetch and
    talks JSON-RPC through the configured gateway. ``httpx`` is imported lazily
    so a pod that never makes an A2A call pays nothing.
    """

    def __init__(
        self,
        registry: RegistryClient,
        *,
        namespace: str = "",
        gateway_base_url: str = "",
        token: str = "",
        timeout_seconds: float = 30.0,
        verify_cards: bool = True,
        allowed_url_suffixes: list[str] | None = None,
    ) -> None:
        self._registry = registry
        self._namespace = namespace
        gateway = urlparse(gateway_base_url.strip())
        self._gateway_origin = (
            f"{gateway.scheme}://{gateway.netloc}" if gateway.scheme in {"http", "https"} and gateway.netloc else ""
        )
        self._token = token
        self._timeout = timeout_seconds
        # Security posture (see verify.py). verify_cards=True is the safe
        # default: a card's signature is checked and its service URL is
        # allowlisted before any call. Relax only for trusted local/dev.
        self._verify = verify_cards
        self._suffixes = allowed_url_suffixes or list(_DEFAULT_URL_SUFFIXES)

    # -- discovery + card fetch --------------------------------------------- #

    def fetch_card(self, name: str, *, namespace: str = "", tag: str = "") -> AgentCard:
        """Fetch and parse one agent's A2A card from the registry."""
        ns = namespace or self._namespace
        try:
            raw = self._registry.get_agent_card(name, namespace=ns, tag=tag)
        except Exception as e:  # noqa: BLE001 — normalize to A2AError for callers
            raise A2AError(f"a2a: cannot fetch card for {name!r}: {e}") from e
        try:
            return AgentCard.from_dict(raw)
        except ValueError as e:
            raise A2AError(f"a2a: malformed card for {name!r}: {e}") from e

    def discover(self, query: str = "", *, namespace: str = "") -> list[AgentCard]:
        """Resolve agent cards, optionally filtered by capability.

        Lists agents from the registry, fetches each card, and keeps the ones
        whose name/description/skills match ``query`` (empty matches all). A
        card that fails to fetch is skipped, not fatal — discovery degrades to
        whatever is currently reachable.
        """
        cards: list[AgentCard] = []
        try:
            agents = self._registry.list_agents()
        except Exception as e:  # noqa: BLE001
            raise A2AError(f"a2a: cannot list agents: {e}") from e
        for a in agents:
            try:
                card = self.fetch_card(a.name, namespace=namespace)
            except A2AError:
                logger.warning("a2a: skipping agent %s — card unavailable", a.name)
                continue
            if card.matches(query):
                cards.append(card)
        return cards

    def find_for_capability(self, capability: str, *, namespace: str = "") -> AgentCard | None:
        """Return the only agent advertising a capability.

        Ambiguity fails closed so a newly published duplicate cannot silently
        replace the product's previous routing choice.
        """
        matches = self.discover(capability, namespace=namespace)
        if len(matches) > 1:
            raise A2AError(f"a2a: ambiguous capability {capability!r}")
        return matches[0] if matches else None

    # -- trust checks ------------------------------------------------------- #

    def verify(self, card: AgentCard) -> None:
        """Run the security guards for a card: registry signature verification
        + service-URL SSRF allowlist. No-op when verification is disabled.
        Raises A2AError when the card must not be trusted/called."""
        if not self._verify:
            return
        verify_card_signature(card, self._registry.get_signing_key())
        check_service_url(card.url, self._suffixes)
        # Suffix allowlist alone misses a hostname that resolves to a
        # private/metadata IP — resolve and IP-block before calling.
        resolve_host_block_check(card.url, self._suffixes)

    # -- invocation --------------------------------------------------------- #

    def send_message(
        self,
        card: AgentCard,
        text: str = "",
        *,
        parts: list[dict[str, Any]] | None = None,
        context_id: str = "",
        task_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Invoke the agent via A2A ``message/send`` and return the raw result.

        Provide ``text`` for the common single-text-part case, or ``parts`` for
        multi-part / non-text messages. The result is the A2A Task or Message
        object (returned verbatim so the orchestrator can interpret it).
        """
        if not card.url:
            raise A2AError(f"a2a: agent {card.name!r} card has no service url to call")
        if not self._gateway_origin:
            raise A2AError("a2a: gateway is not configured")
        # Fail closed: verify the registry signature + allowlist the URL BEFORE
        # we send anything to it.
        self.verify(card)
        if not parts:
            parts = [{"kind": "text", "text": text}]

        message: dict[str, Any] = {
            "role": "user",
            "parts": parts,
            "messageId": uuid.uuid4().hex,
        }
        if context_id:
            message["contextId"] = context_id
        if task_id:
            message["taskId"] = task_id
        if metadata:
            message["metadata"] = metadata

        result = self._rpc(self._gateway_url(card.url), "message/send", {"message": message})
        return result

    def _gateway_url(self, card_url: str) -> str:
        parsed = urlparse(card_url)
        path = parsed.path
        segments = path.split("/")
        if not path.startswith("/a2a/") or ".." in segments or path.startswith("//"):
            raise A2AError("a2a: card does not publish an approved A2A path")
        return f"{self._gateway_origin}{path}"

    def _rpc(self, url: str, method: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            import httpx
        except ImportError as e:  # pragma: no cover
            raise A2AError(f"a2a: httpx not installed: {e}") from e

        payload = {
            "jsonrpc": "2.0",
            "id": uuid.uuid4().hex,
            "method": method,
            "params": params,
        }
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        try:
            r = httpx.post(url, headers=headers, content=json.dumps(payload), timeout=self._timeout)
        except httpx.HTTPError as e:
            raise A2AError("a2a: gateway network error") from e
        if r.status_code >= 400:
            raise A2AError(f"a2a: gateway returned {r.status_code}")
        try:
            body = r.json()
        except json.JSONDecodeError as e:
            raise A2AError("a2a: gateway returned invalid JSON-RPC") from e
        if isinstance(body, dict) and body.get("error"):
            error = body["error"]
            code = error.get("code") if isinstance(error, dict) else "unknown"
            raise A2AError(f"a2a: agent returned error code {code}")
        result = body.get("result") if isinstance(body, dict) else None
        return result if isinstance(result, dict) else {"result": result}


def create_a2a_client(settings: Settings, registry: RegistryClient | None) -> A2AClient | None:
    """Build an A2AClient from settings + a registry client.

    Returns None when no registry client is available (A2A is purely additive —
    the orchestrator runs fine without it). The namespace defaults to the
    configured org so cards resolve in the right tenant/team scope.
    """
    if registry is None:
        return None
    namespace = getattr(settings, "registry_default_tenant", "") or ""
    token = getattr(settings, "llm_gateway_api_key", "") or ""
    gateway_base_url = getattr(settings, "llm_gateway_base_url", "") or ""
    verify_cards = bool(getattr(settings, "a2a_secure", True))
    suffixes = list(getattr(settings, "a2a_allowed_url_suffixes", None) or _DEFAULT_URL_SUFFIXES)
    return A2AClient(
        registry,
        namespace=namespace,
        gateway_base_url=gateway_base_url,
        token=token,
        verify_cards=verify_cards,
        allowed_url_suffixes=suffixes,
    )
