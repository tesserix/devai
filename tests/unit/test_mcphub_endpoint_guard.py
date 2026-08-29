"""The SSRF guard must stay usable for in-cluster downstreams.

``DEVAI_MCP_HUB_SSRF_ENFORCE=true`` turns on DNS resolution in
``check_endpoint_url``. Every in-cluster MCP resolves to a private ClusterIP,
so a blanket private-address rejection would drop the entire federated surface
the moment the flag is flipped — while the metadata endpoint must stay blocked.
"""

from __future__ import annotations

import socket

import pytest

from devai.mcphub.discovery import EndpointGuardError, check_endpoint_url

CLUSTER_SUFFIXES = [".svc.cluster.local", ".svc"]


def _resolves_to(monkeypatch, addr: str) -> None:
    def _fake(host, port, **kwargs):  # noqa: ANN001, ANN202, ARG001
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (addr, port or 80))]

    monkeypatch.setattr(socket, "getaddrinfo", _fake)


def test_allowlisted_cluster_host_may_resolve_private(monkeypatch):
    _resolves_to(monkeypatch, "10.4.7.9")
    check_endpoint_url("http://scrapper-mcp.scrapper.svc.cluster.local:8080/mcp", CLUSTER_SUFFIXES)


def test_allowlisted_host_resolving_to_metadata_is_still_blocked(monkeypatch):
    _resolves_to(monkeypatch, "169.254.169.254")
    with pytest.raises(EndpointGuardError):
        check_endpoint_url("http://evil.svc.cluster.local/mcp", CLUSTER_SUFFIXES)


def test_allowlisted_host_resolving_to_loopback_is_still_blocked(monkeypatch):
    _resolves_to(monkeypatch, "127.0.0.1")
    with pytest.raises(EndpointGuardError):
        check_endpoint_url("http://evil.svc.cluster.local/mcp", CLUSTER_SUFFIXES)


def test_wildcard_allowlist_still_blocks_private_resolution(monkeypatch):
    """No explicit suffix vouched for the host, so private stays blocked."""
    _resolves_to(monkeypatch, "10.4.7.9")
    with pytest.raises(EndpointGuardError):
        check_endpoint_url("http://attacker.example.com/mcp", ["*"])


def test_private_ip_literal_is_blocked_even_when_allowlisted():
    with pytest.raises(EndpointGuardError):
        check_endpoint_url("http://10.4.7.9:8080/mcp", ["*", "10.4.7.9"])
