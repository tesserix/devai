"""Usage ledger key namespacing — global vs per-user (tenant isolation)."""

from devai.analytics.usage_ledger import _ns


def test_namespace_global_vs_user():
    assert _ns() == "devai:usage:"
    assert _ns("a@example.com") == "devai:usage:u:a@example.com:"
    # Two users get disjoint key prefixes — no cross-tenant bleed.
    assert _ns("a@example.com") != _ns("b@example.com")


def test_micro_roundtrip():
    from devai.analytics.usage_ledger import _from_micro, _micro

    assert _micro(1.5) == 1_500_000
    assert _from_micro(_micro(0.123456)) == 0.123456
    assert _micro(-5) == 0  # never negative
