"""Registry-driven discovery + downstream auth injection.

The Hub never hardcodes its downstream topology (docs/agentic/MCP-HUB.md §7,
decision §9): it reads ``kind:MCPServer`` from the registry and federates
whatever is there. Onboarding a new MCP is a *publish*, not a code change.

Two pure concerns live here:
  - :func:`discover` — registry ``McpServer`` records → ``DownstreamSpec`` list.
  - :func:`downstream_headers` — the headers the Hub injects toward a downstream
    per its ``authMode`` (the caller's identity is terminated at the Hub; the
    caller never holds downstream credentials, docs §6.5).

Both take their inputs explicitly (records, a service-token string) so they unit
-test without a live registry or settings object.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from devai.mcphub.model import DownstreamSpec

if TYPE_CHECKING:
    from devai.registry.client import McpServer, RegistryClient

logger = logging.getLogger(__name__)


def _labels_of(record: Any) -> dict[str, str]:
    """Best-effort label extraction.

    The registry client flattens an MCPServer envelope's ``spec`` to the top
    level, so ``metadata.labels`` may live under ``raw['metadata']['labels']``
    (full envelope) or ``raw['labels']`` (some seeds put them in spec). Accept
    both; missing → empty (labels are advisory for server-level grouping).
    """
    raw = getattr(record, "raw", None) or {}
    meta = raw.get("metadata") if isinstance(raw, dict) else None
    if isinstance(meta, dict) and isinstance(meta.get("labels"), dict):
        return {str(k): str(v) for k, v in meta["labels"].items()}
    if isinstance(raw, dict) and isinstance(raw.get("labels"), dict):
        return {str(k): str(v) for k, v in raw["labels"].items()}
    return {}


def spec_from_record(record: McpServer) -> DownstreamSpec:
    """Map one registry ``McpServer`` to a ``DownstreamSpec``.

    Endpoint: ``spec.endpoint`` (design 4.2) or the client's ``url`` field.
    Transport/authMode read from the freeform spec (``raw``), defaulting to the
    safe ``streamable-http`` / ``none``.
    """
    raw = getattr(record, "raw", None) or {}
    endpoint = (raw.get("endpoint") if isinstance(raw, dict) else "") or getattr(record, "url", "") or ""
    transport = (
        (raw.get("transport") if isinstance(raw, dict) else "") or getattr(record, "type", "") or "streamable-http"
    )
    auth_mode = (raw.get("authMode") if isinstance(raw, dict) else "") or "none"
    return DownstreamSpec(
        name=getattr(record, "name", "") or "",
        endpoint=str(endpoint),
        transport=str(transport),
        auth_mode=str(auth_mode),
        labels=_labels_of(record),
        headers={str(k): str(v) for k, v in (getattr(record, "headers", None) or {}).items()},
    )


def discover(client: RegistryClient) -> list[DownstreamSpec]:
    """Read every servable ``kind:MCPServer`` from the registry.

    Unservable specs (no endpoint, or a transport the Hub can't dial — e.g.
    ``stdio``, deferred to a runner adapter) are skipped with a log, never an
    error: the Hub degrades, it doesn't refuse to start (docs decision §9.5).
    """
    out: list[DownstreamSpec] = []
    try:
        records = client.list_mcp_servers()
    except Exception:  # noqa: BLE001 — discovery must not crash the Hub
        logger.warning("mcphub: registry discovery failed; serving empty surface", exc_info=True)
        return out
    for rec in records:
        spec = spec_from_record(rec)
        if not spec.is_servable():
            logger.info(
                "mcphub: skipping non-servable downstream %r (transport=%s endpoint=%s)",
                spec.name,
                spec.transport,
                spec.endpoint or "<none>",
            )
            continue
        out.append(spec)
    logger.info("mcphub: discovered %d servable downstream MCP server(s)", len(out))
    return out


def downstream_headers(spec: DownstreamSpec, *, service_token: str = "") -> dict[str, str]:
    """Headers the Hub injects toward ``spec`` per its ``authMode``.

    - ``none``   → only the spec's static headers (usually empty).
    - ``header`` → the spec's static headers (a pre-shared key/token).
    - ``jwt``    → ``Authorization: Bearer <service_token>`` (+ static headers).
                   The Hub presents its OWN service identity downstream, NOT the
                   caller's — identity is terminated at the edge (docs §6.5).
    - ``mtls``   → no headers; the client certificate is presented by the
                   transport layer, not here.

    Unknown modes degrade to static headers only (never raise).
    """
    headers = dict(spec.headers)
    mode = (spec.auth_mode or "none").lower()
    if mode == "jwt":
        if service_token:
            headers["Authorization"] = f"Bearer {service_token}"
        else:
            logger.warning(
                "mcphub: downstream %r is authMode=jwt but no service token configured; calls may be rejected",
                spec.name,
            )
    elif mode == "mtls":
        # Certificate is presented by the HTTP client/transport, not a header.
        pass
    return headers
