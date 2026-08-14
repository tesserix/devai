"""An agent published to the registry is runnable, not merely advertised.

Local YAML stays authoritative for the roles it defines — those seeds carry
runtime metadata the registry's slimmer schema drops — so the registry only
adds roles that disk does not have. That is exactly the Create-Agent case.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from devai.specializations.service import SpecializationService

_LOCAL_YAML = """
name: db_engineer
display_name: DB Engineer
description: Owns migrations.
llm_provider: claude
system_prompt: You write migrations.
allowed_tools:
  - run_sql
"""


class _Settings:
    specializations_dir = "specializations"


class _FakeRegistry:
    def __init__(self, agents: list[dict[str, Any]] | None = None, *, boom: bool = False) -> None:
        self._agents = agents or []
        self._boom = boom

    def list_skills(self) -> list[Any]:
        return []

    def list_agents(self) -> list[Any]:
        if self._boom:
            raise RuntimeError("registry unreachable")
        return [_FakeAgent(a) for a in self._agents]


class _FakeAgent:
    def __init__(self, raw: dict[str, Any]) -> None:
        self.name = raw.get("name", "")
        self.raw = raw


@pytest.fixture
def local_dir(tmp_path: Path) -> Path:
    (tmp_path / "db_engineer.yaml").write_text(_LOCAL_YAML)
    return tmp_path


def _service(local_dir: Path, registry: Any | None) -> SpecializationService:
    return SpecializationService(_Settings(), directory=local_dir, registry_client=registry)


async def test_a_published_agent_is_resolvable_by_role_name(local_dir: Path) -> None:
    svc = _service(local_dir, _FakeRegistry([{"name": "release-notes-writer", "systemPrompt": "Write notes."}]))
    await svc.start()

    spec = svc.registry.resolve("release_notes_writer")

    assert spec.system_prompt == "Write notes."
    assert spec.metadata["registry_name"] == "release-notes-writer"


async def test_local_yaml_still_wins_for_a_role_that_exists_on_disk(local_dir: Path) -> None:
    svc = _service(local_dir, _FakeRegistry([{"name": "db-engineer", "systemPrompt": "Registry version."}]))
    await svc.start()

    assert svc.registry.resolve("db_engineer").system_prompt == "You write migrations."
    assert svc.registry.resolve("db_engineer").allowed_tools == ["run_sql"]


async def test_an_unusable_published_agent_is_skipped_rather_than_breaking_startup(local_dir: Path) -> None:
    svc = _service(local_dir, _FakeRegistry([{"name": "9-lives"}, {"name": "good-one"}]))
    await svc.start()

    assert svc.registry.has("good_one")
    assert svc.registry.has("db_engineer")


async def test_an_unreachable_registry_leaves_the_local_catalog_running(local_dir: Path) -> None:
    svc = _service(local_dir, _FakeRegistry(boom=True))
    await svc.start()

    assert svc.registry.has("db_engineer")
    assert svc.source == "local"


async def test_the_source_records_that_the_registry_contributed(local_dir: Path) -> None:
    svc = _service(local_dir, _FakeRegistry([{"name": "release-notes-writer"}]))
    await svc.start()

    assert svc.source == "registry+local"


async def test_reload_keeps_the_agents_adopted_from_the_registry(local_dir: Path) -> None:
    svc = _service(local_dir, _FakeRegistry([{"name": "release-notes-writer", "systemPrompt": "Write notes."}]))
    await svc.start()

    await svc.reload()

    assert svc.registry.has("release_notes_writer")
    assert svc.registry.has("db_engineer")


async def test_an_agent_published_after_start_is_runnable_without_a_restart(local_dir: Path) -> None:
    catalog = _FakeRegistry([])
    svc = _service(local_dir, catalog)
    await svc.start()
    assert await svc.resolve_runnable("release_notes_writer") is None

    catalog._agents.append({"name": "release-notes-writer", "systemPrompt": "Write notes."})

    spec = await svc.resolve_runnable("release_notes_writer")
    assert spec is not None
    assert spec.system_prompt == "Write notes."


async def test_resolve_runnable_answers_from_the_catalog_it_already_has(local_dir: Path) -> None:
    svc = _service(local_dir, _FakeRegistry([]))
    await svc.start()

    assert (await svc.resolve_runnable("db_engineer")) is not None
    assert (await svc.resolve_runnable("no_such_role")) is None
