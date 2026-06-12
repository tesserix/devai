"""Registry-driven discovery + downstream auth injection.

The Hub never hardcodes its downstream topology (docs/agentic/MCP-HUB.md §7,
decision §9): it reads ``kind:MCPServer`` from the registry and federates
whatever is there. Onboarding a new MCP is a *publish*, not a code change.

Because that topology is **registry-supplied** (an attacker who can publish an
``MCPServer`` controls the endpoint the Hub dials), every endpoint runs through
:func:`check_endpoint_url` — an SSRF/allowlist guard mirroring
``a2a.check_service_url`` — before it's served or dialed. The Hub's service token
is only injected toward endpoints that pass that guard.

Three pure concerns live here:
  - :func:`check_endpoint_url` — SSRF guard for a registry-supplied endpoint.
  - :func:`discover` — registry ``McpServer`` records → ``DownstreamSpec`` list,
    dropping any whose endpoint fails the guard.
  - :func:`downstream_headers` — the headers the Hub injects toward a downstream
    per its ``authMode`` (the caller's identity is terminated at the Hub; the
    caller never holds downstream credentials, docs §6.5). The service token is
    withheld from endpoints that fail the guard.

All take their inputs explicitly (records, suffix list, a service-token string)
so they unit-test without a live registry or settings object.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from devai.mcphub.model import DownstreamSpec

if TYPE_CHECKING:
    from devai.registry.client import McpServer, RegistryClient

logger = logging.getLogger(__name__)


class EndpointGuardError(ValueError):
    """A registry-supplied endpoint failed the SSRF/allowlist guard."""


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """SSRF-prone address space: loopback/link-local (incl. 169.254.169.254 GKE
    metadata)/private/unspecified/reserved/multicast."""
    return ip.is_loopback or ip.is_link_local or ip.is_private or ip.is_unspecified or ip.is_reserved or ip.is_multicast


def check_endpoint_url(url: str, allowed_suffixes: list[str], *, resolve: bool = True) -> None:
    """SSRF guard for a registry-supplied downstream endpoint.

    Mirrors ``a2a.check_service_url`` (require http/https; block loopback/
    link-local/private/metadata IP literals; gate the host against a suffix
    allowlist) and additionally — when ``resolve`` is true — resolves the host
    and rejects it if ANY resolved address is non-routable. That closes the
    hostname-resolves-to-a-private/metadata-IP gap a suffix allowlist alone
    misses (audit CODE-17). Raises :class:`EndpointGuardError` on rejection so
    callers fail closed.

    A suffix of ``"*"`` disables the allowlist (still blocks non-routable IPs).
    """
    if not url:
        raise EndpointGuardError("mcphub: downstream has no endpoint url to call")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise EndpointGuardError(f"mcphub: unsupported endpoint scheme {parsed.scheme!r} in {url!r} (need http/https)")
    host = parsed.hostname
    if not host:
        raise EndpointGuardError(f"mcphub: endpoint url has no host: {url!r}")

    # Block SSRF-prone IP literals regardless of the allowlist.
    try:
        literal_ip = ipaddress.ip_address(host)
    except ValueError:
        literal_ip = None
    if literal_ip is not None and _is_blocked_ip(literal_ip):
        raise EndpointGuardError(f"mcphub: refusing to dial non-routable address {host} (SSRF guard)")

    # Host-suffix allowlist (a single "*" disables it, IP guards still apply).
    if "*" not in allowed_suffixes:
        host_l = host.lower()
        if not any(
            host_l == (s := suffix.lower()) or host_l == s.lstrip(".") or host_l.endswith(s)
            for suffix in allowed_suffixes
        ):
            raise EndpointGuardError(
                f"mcphub: endpoint host {host!r} is not in the allowed suffix list {allowed_suffixes}; "
                "add it to DEVAI_A2A_ALLOWED_URL_SUFFIXES to permit"
            )

    # Defeat DNS-rebinding / hostname→private-IP pivots: resolve and reject if
    # any address is non-routable. Skipped for IP literals (already checked) and
    # when resolution is disabled (e.g. pure unit tests with offline hosts).
    if resolve and literal_ip is None:
        try:
            infos = socket.getaddrinfo(host, parsed.port or None, proto=socket.IPPROTO_TCP)
        except OSError as e:
            raise EndpointGuardError(f"mcphub: cannot resolve endpoint host {host!r}: {e}") from e
        for info in infos:
            addr = info[4][0]
            try:
                resolved = ipaddress.ip_address(addr)
            except ValueError:
                continue
            if _is_blocked_ip(resolved):
                raise EndpointGuardError(f"mcphub: endpoint host {host!r} resolves to non-routable {addr} (SSRF guard)")


def endpoint_allowed(spec: DownstreamSpec, allowed_suffixes: list[str], *, resolve: bool = True) -> bool:
    """True if ``spec.endpoint`` passes :func:`check_endpoint_url`; logs + False
    otherwise. A convenience wrapper for fail-soft call sites (discovery)."""
    try:
        check_endpoint_url(spec.endpoint, allowed_suffixes, resolve=resolve)
        return True
    except EndpointGuardError as e:
        logger.warning("mcphub: dropping downstream %r — %s", spec.name, e)
        return False


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


def discover(
    client: RegistryClient,
    *,
    allowed_suffixes: list[str] | None = None,
    enforce_ssrf: bool | None = None,
) -> list[DownstreamSpec]:
    """Read every servable ``kind:MCPServer`` from the registry.

    Unservable specs (no endpoint, or a transport the Hub can't dial — e.g.
    ``stdio``, deferred to a runner adapter) are skipped with a log, never an
    error: the Hub degrades, it doesn't refuse to start (docs decision §9.5).

    ``allowed_suffixes`` is the SSRF host-suffix allowlist applied to every
    registry-supplied endpoint via :func:`check_endpoint_url`; ``None`` falls back
    to ``settings.a2a_allowed_url_suffixes`` (the in-cluster service DNS list).

    BEHAVIOR-NEUTRALITY (audit CODE-6): the guard is gated by ``enforce_ssrf``,
    which defaults to ``settings.mcp_hub_ssrf_enforce`` (default OFF) — so today's
    running surface is unchanged. When OFF the guard runs in **audit mode**:
    an endpoint that fails is logged (so operators can see what WOULD be dropped)
    but still served. When ON (recommended in prod), a failing endpoint is
    dropped, same as an unservable one — the Hub fails closed per leg. The
    deliberate flip happens later in chart values.
    """
    if allowed_suffixes is None or enforce_ssrf is None:
        from devai.config import settings

        if allowed_suffixes is None:
            allowed_suffixes = list(settings.a2a_allowed_url_suffixes)
        if enforce_ssrf is None:
            enforce_ssrf = bool(getattr(settings, "mcp_hub_ssrf_enforce", False))

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
        # Cheap scheme/literal/suffix check here; the full DNS-resolution check
        # (which closes the rebinding gap) runs once at the dial site.
        if not endpoint_allowed(spec, allowed_suffixes, resolve=False) and enforce_ssrf:
            continue  # SSRF guard rejected the registry-supplied endpoint (enforce on)
        out.append(spec)
    logger.info("mcphub: discovered %d servable downstream MCP server(s)", len(out))
    return out


def downstream_headers(
    spec: DownstreamSpec,
    *,
    service_token: str = "",
    allowed_suffixes: list[str] | None = None,
    enforce_ssrf: bool = False,
) -> dict[str, str]:
    """Headers the Hub injects toward ``spec`` per its ``authMode``.

    - ``none``   → only the spec's static headers (usually empty).
    - ``header`` → the spec's static headers (a pre-shared key/token).
    - ``jwt``    → ``Authorization: Bearer <service_token>`` (+ static headers).
                   The Hub presents its OWN service identity downstream, NOT the
                   caller's — identity is terminated at the edge (docs §6.5).
    - ``mtls``   → no headers; the client certificate is presented by the
                   transport layer, not here.

    Security (audit CODE-6): the Hub's bearer service token is only attached to
    endpoints that pass :func:`check_endpoint_url`, so a malicious ``MCPServer``
    can't capture the token by pointing ``jwt`` mode at an attacker-controlled or
    metadata endpoint. This withholding is gated by ``enforce_ssrf`` (default OFF
    to stay behavior-neutral): when OFF a failing endpoint is logged but still
    gets the bearer (audit mode); when ON the bearer is withheld. ``allowed_suffixes``
    ``None`` falls back to ``settings.a2a_allowed_url_suffixes``.

    Unknown modes degrade to static headers only (never raise).
    """
    headers = dict(spec.headers)
    mode = (spec.auth_mode or "none").lower()
    if mode == "jwt":
        if not service_token:
            logger.warning(
                "mcphub: downstream %r is authMode=jwt but no service token configured; calls may be rejected",
                spec.name,
            )
        else:
            if allowed_suffixes is None:
                from devai.config import settings

                allowed_suffixes = list(settings.a2a_allowed_url_suffixes)
            passed = endpoint_allowed(spec, allowed_suffixes, resolve=False)
            if passed or not enforce_ssrf:
                if not passed:
                    logger.warning(
                        "mcphub: downstream %r endpoint failed the SSRF guard (audit mode — token still attached; "
                        "set DEVAI_MCP_HUB_SSRF_ENFORCE=true to withhold)",
                        spec.name,
                    )
                headers["Authorization"] = f"Bearer {service_token}"
            else:
                logger.warning(
                    "mcphub: withholding service token from downstream %r — endpoint failed the SSRF guard", spec.name
                )
    elif mode == "mtls":
        # Certificate is presented by the HTTP client/transport, not a header.
        pass
    elif mode in ("gcp_adc", "adc"):
        # Google Cloud MCP servers (aiplatform, agentregistry, bigquery, …)
        # authenticate with an OAuth bearer minted from Application Default
        # Credentials — on GKE that's the pod's Workload Identity GSA. The
        # x-goog-user-project header names the quota/billing project, which
        # impersonated/user-domain ADC tokens don't carry implicitly.
        token, project = _gcp_adc_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
            if project:
                headers.setdefault("x-goog-user-project", project)
        else:
            logger.warning(
                "mcphub: downstream %r is authMode=gcp_adc but ADC is unavailable; calls may be rejected",
                spec.name,
            )
    return headers


_ADC_STATE: dict[str, Any] = {}


def _gcp_adc_token() -> tuple[str, str]:
    """(access_token, quota_project) from cached ADC, or ("", "") when unavailable.

    Lazy-imports google.auth (adapter-family rule: a backend you don't use
    never loads its SDK) and refreshes the cached credentials only when
    expired. Never raises — Google MCP downstreams degrade like any other.
    """
    try:
        creds = _ADC_STATE.get("creds")
        if creds is None:
            import google.auth  # noqa: PLC0415

            creds, adc_project = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            _ADC_STATE["creds"] = creds
            _ADC_STATE["project"] = adc_project or ""
        if not creds.valid:
            from google.auth.transport.requests import Request  # noqa: PLC0415

            creds.refresh(Request())
        from devai.config import settings

        project = getattr(settings, "vertex_project", "") or _ADC_STATE.get("project", "")
        return str(creds.token or ""), str(project)
    except Exception:  # noqa: BLE001
        logger.debug("mcphub: ADC token mint failed", exc_info=True)
        return "", ""
