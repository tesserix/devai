from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "homebrew-release.yaml"


def test_homebrew_release_is_manual_and_uses_pinned_actions() -> None:
    text = WORKFLOW.read_text()
    workflow = yaml.safe_load(text)

    assert workflow[True] == {"workflow_dispatch": workflow[True]["workflow_dispatch"]}
    assert "@v" not in text
    assert "pull_request" not in workflow[True]
    assert "push" not in workflow[True]


def test_homebrew_release_mints_a_tap_only_short_lived_token() -> None:
    text = WORKFLOW.read_text()

    assert "service_account: devai-homebrew-release@tesseracthub-480811.iam.gserviceaccount.com" in text
    assert "repositories: homebrew-tap" in text
    assert "permission-contents: write" in text
    assert "prod-devai-github-app-private-key" in text
    assert "DEVAI_DEPLOY_KEY" not in text


def test_homebrew_release_audits_rendered_formula_by_tap_name() -> None:
    text = WORKFLOW.read_text()

    assert 'tap_root="$(brew --repository)/Library/Taps/tesserix/homebrew-tap"' in text
    assert 'ln -s "$PWD/tap" "$tap_root"' in text
    assert "brew audit --strict tesserix/tap/devai" in text
    assert "brew audit --strict tap/Formula/devai.rb" not in text
