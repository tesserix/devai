"""Repo onboarding — the Repos section of the DevAI platform.

A repo is onboarded when `.platform/devai.yaml` lives on its default
branch. This package surfaces every org repo the GitHub App can see,
opens a gated PR to add the marker, tracks the PR through to merge, and
keeps a Postgres cache that the reconciler rebuilds from the marker files
themselves — so the marker file is always the source of truth.
"""

from devai.onboarding.factory import (
    create_onboarding_service,
    create_onboarding_store,
)
from devai.onboarding.marker import MARKER_PATH, parse_marker, synthesize_marker
from devai.onboarding.models import (
    OnboardedRepo,
    OnboardingMetadata,
    OnboardingState,
    RepoSnapshot,
)
from devai.onboarding.service import OnboardingService
from devai.onboarding.store import (
    InMemoryOnboardingStore,
    OnboardingStore,
    PostgresOnboardingStore,
)

__all__ = [
    "MARKER_PATH",
    "parse_marker",
    "synthesize_marker",
    "OnboardingState",
    "OnboardingMetadata",
    "OnboardedRepo",
    "RepoSnapshot",
    "OnboardingStore",
    "InMemoryOnboardingStore",
    "PostgresOnboardingStore",
    "OnboardingService",
    "create_onboarding_service",
    "create_onboarding_store",
]
