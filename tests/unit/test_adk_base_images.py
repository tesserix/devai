import tomllib
from pathlib import Path

ADK_BASE = (
    "ghcr.io/tesserix/base-python-adk-3.14:weekly@"
    "sha256:4e38ff684b5c9936b855cac13aa71db619de23bca6d379d01e6156c4f402a56b"
)
ADK_VERSION = "0.54.0"
ADK_RELEASE_COMMIT = "07f37da3a38cafbe978d296a2954ab543ee22ce9"


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

    assert f"agent-development-kit@{ADK_RELEASE_COMMIT}" in project
    assert f"agent-development-kit@v{ADK_VERSION}" not in project


def test_application_dependencies_preserve_the_adk_base_constraints() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text())
    dependencies = set(project["project"]["dependencies"])

    assert {
        "google-genai==2.20.0",
        "opentelemetry-api==1.42.1",
        "opentelemetry-sdk==1.42.1",
        "opentelemetry-exporter-otlp-proto-http==1.42.1",
    } <= dependencies


def test_main_ci_installs_the_adk_extra_fail_closed() -> None:
    workflow = Path(".github/workflows/ci.yaml").read_text()

    assert workflow.count('pip install -e ".[dev,kit]"') == 2
    assert workflow.count("GIT_CONFIG_KEY_0") == 2


def test_sre_ci_installs_the_adk_extra_fail_closed() -> None:
    workflow = Path(".github/workflows/sre-build.yaml").read_text()

    assert workflow.count('pip install -e ".[dev,kit]"') == 2
    assert workflow.count("GIT_CONFIG_KEY_0") == 2
