"""Contract tests for the web_search + object_store adapter families, and
the shell/checkpoint/web capability tools.

Per CLAUDE.md rule 6/7: every adapter family ships a Noop and a factory that
never raises, and each backend passes the same contract. The tool tests run
shell_exec + checkpoint against a real temporary git worktree (no cluster).
"""

from __future__ import annotations

import os
import subprocess

import pytest

from devai.adapters.object_store import (
    NoopObjectStoreAdapter,
    create_object_store_adapter,
)
from devai.adapters.web_search import (
    NoopWebSearchAdapter,
    WebSearchResult,
    create_web_search_adapter,
)
from devai.tools import registry as tool_registry


# ── web_search family ──────────────────────────────────────────────────


def test_web_search_factory_never_raises_on_unknown_provider():
    class _Cfg:
        web_search_provider = "does-not-exist"

    adapter = create_web_search_adapter(_Cfg())
    assert isinstance(adapter, NoopWebSearchAdapter)


def test_web_search_factory_defaults_to_noop():
    adapter = create_web_search_adapter(object())
    assert isinstance(adapter, NoopWebSearchAdapter)


def test_web_search_factory_tavily_without_key_degrades_to_noop():
    class _Cfg:
        web_search_provider = "tavily"
        web_search_api_key = ""

    adapter = create_web_search_adapter(_Cfg())
    assert isinstance(adapter, NoopWebSearchAdapter)


@pytest.mark.asyncio
async def test_noop_web_search_contract():
    adapter = NoopWebSearchAdapter()
    assert await adapter.search("anything") == []
    fetched = await adapter.fetch("https://example.com")
    assert isinstance(fetched, WebSearchResult)
    health = await adapter.health_check()
    assert health["ok"] is True


# ── object_store family ────────────────────────────────────────────────


def test_object_store_factory_never_raises():
    class _Cfg:
        object_store_provider = "nonsense"

    assert isinstance(create_object_store_adapter(_Cfg()), NoopObjectStoreAdapter)


def test_object_store_factory_gcs_without_bucket_degrades_to_noop():
    class _Cfg:
        object_store_provider = "gcs"
        object_store_bucket = ""

    assert isinstance(create_object_store_adapter(_Cfg()), NoopObjectStoreAdapter)


@pytest.mark.asyncio
async def test_noop_object_store_roundtrip():
    store = NoopObjectStoreAdapter()
    blob = b"\x89PNG\r\n\x1a\n"  # 8-byte PNG magic
    stored = await store.put("img/1.png", blob, content_type="image/png")
    assert stored.size == 8
    assert await store.get("img/1.png") == blob
    assert await store.exists("img/1.png") is True
    assert await store.exists("missing") is False
    with pytest.raises(KeyError):
        await store.get("missing")


# ── capability tools register + bind ───────────────────────────────────


def test_capability_tools_are_registered():
    for name in ("web_search", "web_fetch", "shell_exec", "checkpoint", "rollback"):
        assert tool_registry.has(name), f"{name} not registered"


@pytest.mark.asyncio
async def test_bind_only_returns_allowed_tools():
    ctx = tool_registry.ToolContext(repo="tesserix/x")
    bound = tool_registry.bind(["shell_exec", "checkpoint", "not_a_tool"], ctx)
    names = [b.spec.name for b in bound]
    assert names == ["shell_exec", "checkpoint"]  # unknown skipped, order preserved


@pytest.mark.asyncio
async def test_shell_exec_refuses_without_workdir():
    ctx = tool_registry.ToolContext()  # no workdir
    bound = {b.spec.name: b.handler for b in tool_registry.bind(["shell_exec"], ctx)}
    out = await bound["shell_exec"]({"command": "echo hi"})
    assert "no sandbox working tree" in out


@pytest.mark.asyncio
async def test_shell_and_checkpoint_against_real_worktree(tmp_path):
    # init a git repo so checkpoint/rollback have something to commit to
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "a.txt").write_text("one")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)

    ctx = tool_registry.ToolContext(workdir=str(tmp_path))
    tools = {b.spec.name: b.handler for b in tool_registry.bind(["shell_exec", "checkpoint", "rollback"], ctx)}

    # shell_exec runs in the worktree
    out = await tools["shell_exec"]({"command": "echo hello && cat a.txt"})
    assert "exit_code=0" in out
    assert "hello" in out and "one" in out

    # make a change, checkpoint it
    (tmp_path / "a.txt").write_text("two")
    cp = await tools["checkpoint"]({"label": "set to two"})
    assert "checkpoint created" in cp
    sha = cp.split(":")[1].strip().split(" ")[0]
    assert os.path.exists(tmp_path / "a.txt")

    # change again, then roll back to the checkpoint
    (tmp_path / "a.txt").write_text("three")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "three"], cwd=tmp_path, check=True)
    rb = await tools["rollback"]({"sha": sha})
    assert "rolled back" in rb
    assert (tmp_path / "a.txt").read_text() == "two"
