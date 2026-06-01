"""Tests for the SSRF guard used by agent URL-fetch tools."""

import pytest

from devai.tools.url_guard import UnsafeURLError, assert_public_url


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://127.0.0.1/",  # loopback
        "https://127.0.0.1:8080/admin",
        "http://10.0.0.5/",  # RFC1918
        "http://192.168.1.1/",
        "http://172.16.0.1/",
        "http://[::1]/",  # IPv6 loopback
        "http://0.0.0.0/",  # unspecified
        "http://localhost/",  # resolves to loopback
    ],
)
def test_blocks_internal_targets(url):
    with pytest.raises(UnsafeURLError):
        assert_public_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/file",
        "file:///etc/passwd",
        "gopher://example.com/",
        "data:text/plain;base64,AAAA",
    ],
)
def test_blocks_non_http_schemes(url):
    with pytest.raises(UnsafeURLError):
        assert_public_url(url)


def test_rejects_missing_host():
    with pytest.raises(UnsafeURLError):
        assert_public_url("http:///nohost")


def test_allows_public_ip_literal():
    # A public IP literal needs no DNS and must pass.
    assert assert_public_url("http://8.8.8.8/") is None
    assert assert_public_url("https://1.1.1.1/resolve") is None
