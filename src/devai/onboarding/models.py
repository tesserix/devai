"""Domain models for repo onboarding.

A repo is *onboarded* when `.platform/devai.yaml` lives on its default
branch — that file is the single source of truth. The Postgres store is
only a cache that the reconciler rebuilds from the marker files, so the
platform survives a DB wipe without losing onboarded state.

State machine:

    discovered ──onboard()──► pending_pr ──pr merged──► onboarded
        ▲                         │                        │
        └────── pr closed ────────┘                 marker deleted
                                                           ▼
                                                        dormant
                                          archive() ──► archived
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from datetime import datetime


class OnboardingState(StrEnum):
    """Lifecycle of a repo inside the DevAI platform."""

    DISCOVERED = "discovered"      # seen in the org catalog, not enrolled
    PENDING_PR = "pending_pr"      # onboarding PR open, awaiting merge
    ONBOARDED = "onboarded"        # marker present on the default branch
    ARCHIVED = "archived"          # soft-deleted by an operator
    DORMANT = "dormant"            # was onboarded, marker deleted out-of-band

    def __str__(self) -> str:  # so f-strings / DB writes use the value
        return self.value


@dataclass(slots=True)
class OnboardingMetadata:
    """The `onboarding:` block written into `.platform/devai.yaml`.

    Deliberately minimal — no stack detection. Any repo (empty, docs,
    Java, Rust, anything) onboards identically. Runtime hints are
    optional and filled in later by an operator or an agent.
    """

    version: int = 1
    onboarded_at: str = ""          # ISO-8601 string (kept as text in the marker)
    onboarded_by: str = ""
    default_base_branch: str = "main"
    description: str = ""


@dataclass(slots=True)
class RepoSnapshot:
    """One row of the org catalog (what the Repos table renders)."""

    owner: str
    name: str
    default_branch: str = "main"
    description: str = ""
    language: str = ""
    private: bool = False
    archived: bool = False
    html_url: str = ""
    pushed_at: str = ""
    # Onboarding overlay — filled by the service from the marker probe
    # and/or the store. `state` is the resolved lifecycle state.
    has_marker: bool | None = None
    state: OnboardingState = OnboardingState.DISCOVERED
    pr_number: int | None = None
    pr_url: str = ""
    draft: bool = False

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "owner": self.owner,
            "name": self.name,
            "full_name": self.full_name,
            "default_branch": self.default_branch,
            "description": self.description,
            "language": self.language,
            "private": self.private,
            "archived": self.archived,
            "html_url": self.html_url,
            "pushed_at": self.pushed_at,
            "has_marker": self.has_marker,
            "state": str(self.state),
            "onboarded": self.state == OnboardingState.ONBOARDED,
            "pr_number": self.pr_number,
            "pr_url": self.pr_url,
            "draft": self.draft,
        }


@dataclass(slots=True)
class OnboardedRepo:
    """A persisted onboarding record (the DB-cache row)."""

    owner: str
    name: str
    state: OnboardingState = OnboardingState.DISCOVERED
    pr_number: int | None = None
    pr_url: str = ""
    draft: bool = False             # onboarding PR opens as draft, until marked ready
    default_base_branch: str = "main"
    description: str = ""
    detected_stack: dict[str, Any] = field(default_factory=dict)
    onboarded_at: datetime | None = None
    onboarded_by: str = ""
    tags: list[str] = field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "owner": self.owner,
            "name": self.name,
            "full_name": self.full_name,
            "state": str(self.state),
            "onboarded": self.state == OnboardingState.ONBOARDED,
            "pr_number": self.pr_number,
            "pr_url": self.pr_url,
            "draft": self.draft,
            "default_base_branch": self.default_base_branch,
            "description": self.description,
            "detected_stack": self.detected_stack,
            "onboarded_at": self.onboarded_at.isoformat() if self.onboarded_at else None,
            "onboarded_by": self.onboarded_by,
            "tags": self.tags,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
