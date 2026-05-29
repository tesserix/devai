"""Synthesise and parse the `.platform/devai.yaml` onboarding marker.

The marker's *presence* on the default branch is what makes a repo
onboarded. Its contents carry an `onboarding:` metadata block plus the
existing `PlatformConfig` shape (default blueprint + lane labels) so the
Workflows kanban keeps working unchanged.

`parse_marker` is tolerant: a pre-existing `.platform/devai.yaml` written
before onboarding metadata existed still parses as *found* (pre-versioned)
with empty metadata, so the reconciler surfaces it as onboarded rather
than re-opening a PR.
"""

from __future__ import annotations

from typing import Any

import yaml

from devai.onboarding.models import OnboardingMetadata

# Path probed / written to decide onboarding. Single source of truth.
MARKER_PATH = ".platform/devai.yaml"

# Default lane → label map mirrors devai.scm.routes._LANE_LABELS so the
# seeded marker matches what the kanban classifier expects.
_DEFAULT_LANES: dict[str, list[str]] = {
    "queued": ["queued", "todo", "backlog"],
    "in_progress": ["in-progress", "wip", "doing"],
    "review": ["review", "in-review"],
    "deployed": ["deployed", "staging", "in-staging"],
    "shipped": ["shipped", "released", "production"],
}


def synthesize_marker(metadata: OnboardingMetadata) -> str:
    """Render the `.platform/devai.yaml` body for a freshly onboarded repo.

    Built as a structured dict then dumped with PyYAML so user-supplied
    strings (a login with a colon, a branch with a slash, a description
    with quotes) are quoted safely.
    """
    doc: dict[str, Any] = {
        "apiVersion": "devai.tesserix.app/v1alpha1",
        "kind": "PlatformConfig",
        "metadata": {"name": "devai"},
        "onboarding": {
            "version": metadata.version,
            "onboardedAt": metadata.onboarded_at,
            "onboardedBy": metadata.onboarded_by,
            "defaultBaseBranch": metadata.default_base_branch,
            "description": metadata.description,
        },
        "spec": {
            "defaultBlueprint": "default",
            "lanes": _DEFAULT_LANES,
            "allowedSpecialisations": [],
        },
    }
    header = (
        "# DevAI platform marker — managed by the Repos onboarding flow.\n"
        "# Presence of this file on the default branch enrols the repo in\n"
        "# DevAI. Edit `onboarding.description` for a one-line summary; the\n"
        "# `spec` block tunes the default blueprint and Workflows lanes.\n"
    )
    return header + yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)


def parse_marker(text: str) -> tuple[OnboardingMetadata, bool]:
    """Parse a marker body into metadata.

    Returns ``(metadata, found)``:

      * ``found=True``  — the body is a DevAI marker (has the apiVersion,
        a PlatformConfig kind, or an `onboarding:` block). Metadata is
        populated from the `onboarding:` block when present, else left at
        defaults (pre-versioned file).
      * ``found=False`` — the body is not a DevAI marker (empty, unrelated
        YAML, or unparseable). The reconciler treats this as "no marker".
    """
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        return OnboardingMetadata(), False

    if not isinstance(doc, dict):
        return OnboardingMetadata(), False

    api_version = str(doc.get("apiVersion", ""))
    kind = str(doc.get("kind", ""))
    onboarding = doc.get("onboarding")
    is_marker = (
        api_version.startswith("devai.tesserix.app")
        or kind == "PlatformConfig"
        or isinstance(onboarding, dict)
    )
    if not is_marker:
        return OnboardingMetadata(), False

    if not isinstance(onboarding, dict):
        # Pre-versioned marker — no onboarding block. Still onboarded.
        return OnboardingMetadata(version=0), True

    meta = OnboardingMetadata(
        version=int(onboarding.get("version", 1) or 1),
        onboarded_at=str(onboarding.get("onboardedAt", "") or ""),
        onboarded_by=str(onboarding.get("onboardedBy", "") or ""),
        default_base_branch=str(onboarding.get("defaultBaseBranch", "main") or "main"),
        description=str(onboarding.get("description", "") or ""),
    )
    return meta, True
