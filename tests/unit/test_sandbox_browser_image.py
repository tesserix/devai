"""The browser leg ships independently without bloating every runner."""

from pathlib import Path


def test_browser_image_contains_chromium_and_the_loopback_desktop_stack() -> None:
    dockerfile = Path("Dockerfile.runner").read_text()

    assert "AS browser" in dockerfile
    for package in ("xvfb", "fluxbox", "x11vnc", "novnc", "websockify"):
        assert package in dockerfile.lower()
    assert "playwright install" in dockerfile
    assert "chromium" in dockerfile
    assert "seccomp=unconfined" not in dockerfile.lower()


def test_runner_workflow_builds_both_runner_and_browser_targets() -> None:
    workflow = Path(".github/workflows/runner-build.yaml").read_text()

    assert "target: runner" in workflow
    assert "target: browser" in workflow
    assert "image: devai-runner" in workflow
    assert "image: devai-browser" in workflow
    assert "src/devai/sandbox/**" in workflow


def test_every_main_commit_gets_a_browser_image_tag_before_deploy() -> None:
    workflow = Path(".github/workflows/ensure-image-tags.yaml").read_text()

    images = workflow.split('IMAGES="', 1)[1].split('"', 1)[0].split()
    assert "devai-browser" in images
