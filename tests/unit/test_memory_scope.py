"""Phase 4.3 — per-tenant memory scoping.

The memory scope key is ``<tenant>::<repo>`` so one tenant's learnings don't
leak into another's recalls on the same repo. A user's run is scoped to their
team (teammates share) or to themselves; webhook/system runs are repo-only
(isolated by repo ownership; their per-event identity would over-fragment)."""

from __future__ import annotations

from devai.pipeline.stages.memory_scope import run_tenant, tenant_scoped_repo
from devai.pipeline.types import DevAITask


def _task(repo: str = "tesserix/devai", **principal) -> DevAITask:
    t = DevAITask(intent="x", repo=repo)
    t.principal = principal or None
    return t


def test_team_member_scopes_to_team() -> None:
    t = _task(email="a@x.com", auth_provider="google", team_ids=["acme", "beta"])
    assert tenant_scoped_repo(t) == "team:acme::tesserix/devai"  # first sorted team — teammates share


def test_solo_user_scopes_to_email() -> None:
    assert tenant_scoped_repo(_task(email="a@x.com", auth_provider="google")) == "a@x.com::tesserix/devai"


def test_webhook_run_is_repo_only() -> None:
    t = _task(email="webhook:tesserix/devai#42", auth_provider="webhook")
    assert run_tenant(t) == "" and tenant_scoped_repo(t) == "tesserix/devai"


def test_system_run_is_repo_only() -> None:
    assert tenant_scoped_repo(_task(email="system", auth_provider="system")) == "tesserix/devai"


def test_no_principal_is_global() -> None:
    assert tenant_scoped_repo(_task()) == "tesserix/devai"  # back-compat: bare repo


def test_two_users_isolated_on_same_repo() -> None:
    a = _task(repo="shared/repo", email="a@x.com", auth_provider="google")
    b = _task(repo="shared/repo", email="b@y.com", auth_provider="google")
    assert tenant_scoped_repo(a) != tenant_scoped_repo(b)  # no cross-tenant leak


def test_teammates_share_across_users() -> None:
    a = _task(email="a@x.com", auth_provider="google", team_ids=["acme"])
    b = _task(email="b@x.com", auth_provider="google", team_ids=["acme"])
    assert tenant_scoped_repo(a) == tenant_scoped_repo(b)  # same team → shared learnings
