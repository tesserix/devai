from pathlib import Path

ADK_BASE = (
    "ghcr.io/tesserix/base-python-adk-3.14:20260829@"
    "sha256:5a6fd1863ed7f37f3929cc596d0ec063c3077c11713cd334f14d1df2b30ef386"
)
ADK_VERSION = "0.53.1"


def test_agent_images_use_the_verified_adk_base() -> None:
    for dockerfile in ("Dockerfile", "Dockerfile.runner", "Dockerfile.sre"):
        source = Path(dockerfile).read_text()

        assert ADK_BASE in source
        assert "python:3.12-slim" not in source
        assert "kit unavailable" not in source
        assert "python -m pip check" in source
        assert f"m.version('tesserix-adk') == '{ADK_VERSION}'" in source


def test_kit_extra_pins_the_same_adk_release() -> None:
    project = Path("pyproject.toml").read_text()

    assert f"agent-development-kit@v{ADK_VERSION}" in project


def test_main_ci_installs_the_adk_extra_fail_closed() -> None:
    workflow = Path(".github/workflows/ci.yaml").read_text()

    assert workflow.count('pip install -e ".[dev,kit]"') == 2
    assert workflow.count("GIT_CONFIG_KEY_0") == 2


def test_sre_ci_installs_the_adk_extra_fail_closed() -> None:
    workflow = Path(".github/workflows/sre-build.yaml").read_text()

    assert workflow.count('pip install -e ".[dev,kit]"') == 2
    assert workflow.count("GIT_CONFIG_KEY_0") == 2
