"""Production promotion only advances after every image is pullable."""

from pathlib import Path

WORKFLOW = Path(".github/workflows/ensure-image-tags.yaml")


def test_failed_or_timed_out_image_build_blocks_promotion() -> None:
    workflow = WORKFLOW.read_text()

    assert '.conclusion != \\"success\\"' in workflow
    assert "image build workflow(s) failed" in workflow
    assert "timed out waiting for image builds" in workflow
    assert "2>/dev/null || echo 0" not in workflow


def test_missing_ghcr_image_blocks_promotion() -> None:
    workflow = WORKFLOW.read_text()

    assert "missing required image" in workflow
    assert "missing=1" in workflow
    assert '[ "$missing" -eq 0 ]' in workflow


def test_auth_bff_and_mcp_bridge_are_verified_in_direct_gar() -> None:
    workflow = WORKFLOW.read_text()

    assert "GAR_REGISTRY: asia-south1-docker.pkg.dev" in workflow
    assert 'DIRECT_GAR_IMAGES="devai-auth-bff devai-mcp-bridge"' in workflow
    assert 'crane copy "$source" "$target"' in workflow
    assert 'crane manifest "$target"' in workflow
    assert workflow.index('crane manifest "$target"') < workflow.index("Advance the deploy marker")
