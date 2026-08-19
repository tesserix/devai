"""Usage ledger key namespacing — global vs per-user (tenant isolation)."""

import fakeredis.aioredis

from devai.analytics.usage_ledger import _ns


def test_namespace_global_vs_user():
    assert _ns() == "devai:usage:"
    assert _ns("a@example.com") == "devai:usage:u:a@example.com:"
    # Two users get disjoint key prefixes — no cross-tenant bleed.
    assert _ns("a@example.com") != _ns("b@example.com")


def test_namespace_qualifies_tenant_and_subject():
    assert _ns(tenant="tenant-a") == "devai:usage:t:tenant-a:"
    assert _ns("shared-uid", tenant="tenant-a") == "devai:usage:t:tenant-a:u:shared-uid:"
    assert _ns("shared-uid", tenant="tenant-a") != _ns("shared-uid", tenant="tenant-b")


def test_micro_roundtrip():
    from devai.analytics.usage_ledger import _from_micro, _micro

    assert _micro(1.5) == 1_500_000
    assert _from_micro(_micro(0.123456)) == 0.123456
    assert _micro(-5) == 0  # never negative


async def test_same_subject_in_two_tenants_has_disjoint_cost_rollups():
    from devai.analytics.usage_ledger import UsageLedger

    ledger = UsageLedger("")
    ledger._redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    for tenant, cost in (("tenant-a", 1.25), ("tenant-b", 2.5)):
        await ledger.record(
            day="2026-08-18",
            provider="gateway",
            model="claude-sonnet-4-6",
            tokens_in=10,
            tokens_out=5,
            cost_usd=cost,
            duration_ms=20,
            triggered_by="same@example.com",
            tenant_id=tenant,
            user_id="shared-uid",
        )

    assert (await ledger.summary("shared-uid", "tenant-a"))["cost_usd"] == 1.25
    assert (await ledger.summary("shared-uid", "tenant-b"))["cost_usd"] == 2.5
    assert [row["tenant_id"] for row in await ledger.by_user("tenant-a")] == ["tenant-a"]


async def test_sandbox_cost_rollups_are_tenant_and_user_scoped():
    from devai.analytics.usage_ledger import UsageLedger

    ledger = UsageLedger("")
    ledger._redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    for sandbox_id, user_id, cost in (("sb-a", "alice", 1.25), ("sb-b", "bob", 2.5)):
        await ledger.record(
            day="2026-08-19",
            provider="anthropic",
            model="claude-sonnet-4-6",
            tokens_in=10,
            tokens_out=5,
            cost_usd=cost,
            duration_ms=20,
            tenant_id="tenant-a",
            user_id=user_id,
            sandbox_id=sandbox_id,
        )

    assert [row["sandbox_id"] for row in await ledger.by_sandbox("tenant-a", "alice")] == ["sb-a"]
    assert {row["sandbox_id"] for row in await ledger.by_sandbox("tenant-a")} == {"sb-a", "sb-b"}
    assert await ledger.by_sandbox("tenant-b") == []
