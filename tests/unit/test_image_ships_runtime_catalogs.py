"""The ALM image must ship every catalog directory the runtime loads by name.

`Settings` addresses blueprints, specializations and crews as bare relative
paths resolved against the container's WORKDIR. A directory that is not COPYd
into the image therefore loads as *empty* rather than failing — crews shipped
this way for a while, and every dynamic crew pick silently resolved to nothing.
"""

from pathlib import Path

from devai.config import Settings

DOCKERFILE = Path("Dockerfile")
WORKDIR = "/app"

# config field → the repo directory it names
_CATALOGS = {
    "pipeline_blueprint_dir": "blueprints",
    "specializations_dir": "specializations",
    "crews_dir": "crews",
}


def test_every_catalog_default_is_a_relative_directory_that_exists() -> None:
    settings = Settings()

    for field, directory in _CATALOGS.items():
        value = getattr(settings, field)
        assert value == directory, f"{field} default drifted from {directory!r}"
        assert Path(directory).is_dir(), f"{directory}/ is missing from the repo"


def test_the_image_copies_every_catalog_into_the_workdir() -> None:
    source = DOCKERFILE.read_text()

    assert f"WORKDIR {WORKDIR}" in source
    for directory in _CATALOGS.values():
        assert f"COPY {directory}/ {WORKDIR}/{directory}/" in source, (
            f"{directory}/ is never copied into the image, so it loads empty at runtime"
        )


def test_the_runner_image_ships_specializations_outside_the_workspace_mount() -> None:
    """The runner Job mounts an emptyDir at /devai/work, shadowing anything
    the image ships under it — the catalog must live elsewhere and be
    addressed explicitly via DEVAI_SPECIALIZATIONS_DIR."""
    source = Path("Dockerfile.runner").read_text()

    assert "COPY --chown=10001:10001 specializations/ /devai/specializations/" in source
    assert "ENV DEVAI_SPECIALIZATIONS_DIR=/devai/specializations" in source
    assert "/devai/work/specializations" not in source, (
        "specializations under /devai/work are shadowed by the workspace emptyDir mount"
    )
