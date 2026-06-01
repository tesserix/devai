"""SSRF guard for agent-supplied URLs.

Agent tools (e.g. ``doc_read_url``) fetch URLs that can originate from
prompt-injected requirement text. Without a guard, a crafted document
could make an agent reach the cloud metadata endpoint
(``169.254.169.254``), an in-cluster Service, or any RFC1918 host — a
classic SSRF. This module validates that a URL is plain HTTP(S) and that
every hostname resolves only to public, routable addresses.

Use :func:`assert_public_url` before issuing a request, and re-validate
every redirect ``Location`` (a public URL can 302 to an internal one).
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

# The link-local block that carries the cloud metadata service on GCP, AWS,
# and Azure. ``ipaddress.is_link_local`` already covers 169.254.0.0/16, but
# we name it explicitly so the intent is obvious.
_METADATA_IP = ipaddress.ip_address("169.254.169.254")

_ALLOWED_SCHEMES = {"http", "https"}


class UnsafeURLError(ValueError):
    """Raised when a URL is rejected by the SSRF guard."""


def _ip_is_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Reject anything that isn't a globally-routable unicast address."""
    return (
        ip == _METADATA_IP
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        # IPv4-mapped IPv6 (e.g. ::ffff:127.0.0.1) — unwrap and re-check.
        or (getattr(ip, "ipv4_mapped", None) is not None and _ip_is_blocked(ip.ipv4_mapped))  # type: ignore[union-attr]
    )


def assert_public_url(url: str) -> None:
    """Validate ``url`` is safe to fetch, or raise :class:`UnsafeURLError`.

    Checks the scheme is http/https and that *every* address the hostname
    resolves to is public — so a hostname with one private A record can't
    slip through. DNS is resolved here; callers should pass the resolved
    URL straight to the HTTP client to keep the check meaningful (a small
    TOCTOU window remains, but it closes the obvious holes).
    """
    parts = urlsplit(url)
    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        raise UnsafeURLError(f"scheme {parts.scheme!r} not allowed (use http/https)")

    host = parts.hostname
    if not host:
        raise UnsafeURLError("URL has no host")

    # A literal IP in the URL never needs DNS — validate it directly.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if _ip_is_blocked(literal):
            raise UnsafeURLError(f"host {host} resolves to a non-public address")
        return

    try:
        infos = socket.getaddrinfo(host, parts.port or None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise UnsafeURLError(f"could not resolve host {host!r}: {e}") from e

    if not infos:
        raise UnsafeURLError(f"host {host!r} did not resolve")

    for info in infos:
        sockaddr = info[4]
        ip = ipaddress.ip_address(sockaddr[0])
        if _ip_is_blocked(ip):
            raise UnsafeURLError(f"host {host} resolves to a non-public address ({ip})")


__all__ = ["assert_public_url", "UnsafeURLError"]
