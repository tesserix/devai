"""Tests for the tool dispatcher that bridges allowed_tools → LLM tools."""

from __future__ import annotations

from devai.adapters.llm.base import ToolSpec
from devai.tools.dispatch import ToolDispatcher


def test_build_tool_specs_resolves_known_and_drops_unknown():
    d = ToolDispatcher(scm=None)
    specs = d.build_tool_specs(["scm_list_files", "totally_made_up_tool"])
    names = [s.name for s in specs]
    assert "totally_made_up_tool" not in names
    assert names == ["scm_list_files"]
    spec = specs[0]
    assert isinstance(spec, ToolSpec)
    assert isinstance(spec.parameters, dict)  # carries the tool's input_schema


def test_build_tool_specs_empty_list():
    assert ToolDispatcher(scm=None).build_tool_specs([]) == []


async def test_execute_unknown_tool_returns_error_string():
    d = ToolDispatcher(scm=None)
    out = await d.execute("no_such_tool", {})
    assert out == "error: unknown tool 'no_such_tool'"


async def test_execute_routes_to_executor_and_stringifies_dict():
    """execute() must find the right executor, call it, and return a string
    (JSON-encoding dict results) — verified with a fake executor wired into
    the dispatcher's internal maps so the test is deterministic."""

    class _FakeExec:
        async def execute(self, name, arguments):
            return {"called": name, "args": arguments}

    d = ToolDispatcher(scm=None)
    d._index["fake_tool"] = ({}, "fake.module")
    d._executors["fake.module"] = _FakeExec()

    out = await d.execute("fake_tool", {"x": 1})
    assert '"called": "fake_tool"' in out
    assert '"x": 1' in out


async def test_execute_swallows_executor_exception():
    class _BoomExec:
        async def execute(self, name, arguments):
            raise RuntimeError("kaboom")

    d = ToolDispatcher(scm=None)
    d._index["boom_tool"] = ({}, "boom.module")
    d._executors["boom.module"] = _BoomExec()

    out = await d.execute("boom_tool", {})
    assert out.startswith("error: tool boom_tool failed")
    assert "kaboom" in out


async def test_execute_handles_missing_executor():
    # A module that imports fine but exposes no *ToolExecutor class.
    d = ToolDispatcher(scm=None)
    d._index["orphan_tool"] = ({}, "json")
    out = await d.execute("orphan_tool", {})
    assert out == "error: no executor for tool 'orphan_tool'"
