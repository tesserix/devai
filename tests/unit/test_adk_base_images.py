from pathlib import Path

ADK_BASE = (
    "ghcr.io/tesserix/base-python-adk-3.13:20260820@"
    "sha256:cca20646be7d01045fe3fa4c411cdaff8df600da7a3d7769b9786b5282d18f9a"
)


def test_agent_images_use_the_verified_adk_base() -> None:
    for dockerfile in ("Dockerfile", "Dockerfile.runner", "Dockerfile.sre"):
        source = Path(dockerfile).read_text()

        assert ADK_BASE in source
        assert "python:3.12-slim" not in source
        assert "kit unavailable" not in source
        assert "python -m pip check" in source


def test_main_ci_installs_the_adk_extra_fail_closed() -> None:
    workflow = Path(".github/workflows/ci.yaml").read_text()

    assert workflow.count('pip install -e ".[dev,kit]"') == 2
    assert workflow.count("GIT_CONFIG_KEY_0") == 2


def test_sre_ci_installs_the_adk_extra_fail_closed() -> None:
    workflow = Path(".github/workflows/sre-build.yaml").read_text()

    assert workflow.count('pip install -e ".[dev,kit]"') == 2
    assert workflow.count("GIT_CONFIG_KEY_0") == 2
