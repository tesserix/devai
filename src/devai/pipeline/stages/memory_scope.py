"""Per-tenant scoping for agent memory.

In a multi-tenant deployment the same repo path is a *different context* per
tenant — so a memory's scope key must include the tenant, or one tenant's
learnings leak into another's recalls on the same repo. The scope key is
``<tenant>::<repo>``; a run with no tenant uses the bare repo.

Tenant resolution (from the run's `Principal`):
  - a real user's run → their **team** (so teammates share learnings), else the
    individual user (email/uid);
  - a **webhook / system** run → no tenant (repo-only): it's triggered by
    activity *on* a repo and already isolated by who owns that repo, and its
    synthetic per-event identity would otherwise over-fragment memory.

This is intentionally a thin call-site convention over the existing ``repo``
scope (every MemoryAdapter already stores + filters by it), so it needs no ABC
or schema change. A future refactor could promote ``tenant`` to a first-class
MemoryRecord field if richer cross-repo-within-tenant recall is needed.
"""

from __future__ import annotations

from devai.pipeline.types import DevAITask

_GLOBAL_AUTH = frozenset({"webhook", "system"})


def run_tenant(task: DevAITask) -> str:
    """The tenant a run's memory belongs to, or '' for repo-only (global) scope."""
    p = task.principal or {}
    if not p or p.get("auth_provider") in _GLOBAL_AUTH:
        return ""
    teams = p.get("team_ids") or []
    if teams:
        # Deterministic single key even for multi-team users; teammates share.
        return f"team:{sorted(teams)[0]}"
    return str(p.get("email") or p.get("uid") or "").strip()


def tenant_scoped_repo(task: DevAITask) -> str:
    """Memory scope key for a run: ``<tenant>::<repo>``, or the bare repo when
    the run is repo-global. Stable for a given (tenant, repo) so writes and
    recalls within a tenant line up, and disjoint across tenants on one repo."""
    repo = task.repo or "global"
    tenant = run_tenant(task)
    return f"{tenant}::{repo}" if tenant else repo


__all__ = ["run_tenant", "tenant_scoped_repo"]
