"""Marker synth/parse — pure functions, the foundation of onboarding."""

from __future__ import annotations

from devai.onboarding.marker import MARKER_PATH, parse_marker, synthesize_marker
from devai.onboarding.models import OnboardingMetadata


def test_marker_path_is_platform_devai_yaml() -> None:
    assert MARKER_PATH == ".platform/devai.yaml"


def test_synthesize_then_parse_round_trips() -> None:
    meta = OnboardingMetadata(
        onboarded_at="2026-05-29T10:00:00+00:00",
        onboarded_by="samyak.rout@gmail.com",
        default_base_branch="develop",
        description="A test repo",
    )
    body = synthesize_marker(meta)
    parsed, found = parse_marker(body)
    assert found is True
    assert parsed.onboarded_by == "samyak.rout@gmail.com"
    assert parsed.default_base_branch == "develop"
    assert parsed.description == "A test repo"
    assert parsed.version == 1


def test_synthesize_quotes_tricky_user_strings() -> None:
    # A login with a colon and a branch with a slash must not break YAML.
    meta = OnboardingMetadata(
        onboarded_by="user: weird",
        default_base_branch="release/2026",
        description='has "quotes" and: colons',
    )
    parsed, found = parse_marker(synthesize_marker(meta))
    assert found is True
    assert parsed.onboarded_by == "user: weird"
    assert parsed.default_base_branch == "release/2026"
    assert parsed.description == 'has "quotes" and: colons'


def test_synthesize_keeps_platformconfig_shape() -> None:
    body = synthesize_marker(OnboardingMetadata())
    assert "apiVersion: devai.tesserix.app/v1alpha1" in body
    assert "kind: PlatformConfig" in body
    assert "lanes:" in body  # Workflows kanban still reads this


def test_parse_pre_versioned_marker_is_found_with_version_zero() -> None:
    # An existing marker written before onboarding metadata existed.
    legacy = (
        "apiVersion: devai.tesserix.app/v1alpha1\n"
        "kind: PlatformConfig\n"
        "metadata:\n  name: devai\n"
        "spec:\n  defaultBlueprint: default\n"
    )
    meta, found = parse_marker(legacy)
    assert found is True
    assert meta.version == 0


def test_parse_unrelated_yaml_is_not_a_marker() -> None:
    meta, found = parse_marker("name: something\nversion: 2\n")
    assert found is False


def test_parse_empty_or_garbage_is_not_a_marker() -> None:
    assert parse_marker("")[1] is False
    assert parse_marker("\t::: not yaml :::\n  - [")[1] is False
