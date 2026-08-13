"""Tool gateway modes — real / mock / replay / block (#181).

The property under test: isolating a *process* is not enough for an agent,
because the agent chooses its actions at runtime. What has to be isolated is the
side effect, and the default has to be safe — running a 500-case eval suite must
not be able to issue 500 real refunds.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from devai.sandbox.gateway import (
    ToolCallRecord,
    ToolGateway,
    is_side_effecting,
)
from devai.sandbox.models import ToolMode, ToolPolicy


async def _real(result: str = "did the thing") -> str:
    return result


def _gateway(**kw: Any) -> ToolGateway:
    return ToolGateway(**kw)


# ── classification ────────────────────────────────────────────────────


def test_known_write_tools_are_side_effecting() -> None:
    assert is_side_effecting("scm_merge_pull_request")
    assert is_side_effecting("kubectl_scale")
    assert is_side_effecting("argocd_sync")


def test_read_only_tools_are_not() -> None:
    assert not is_side_effecting("scm_read_file")
    assert not is_side_effecting("kubectl_get_pods")


def test_an_unknown_tool_is_treated_as_side_effecting() -> None:
    # An MCP tool nobody classified could be `refund_customer`. Guess safe.
    assert is_side_effecting("acme_mcp_do_whatever")


# ── safe defaults ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_default_policy_mocks_everything() -> None:
    gw = _gateway()
    out = await gw.call("scm_read_file", {"path": "README.md"}, _real)

    assert "[mock]" in out
    assert gw.records[0].mode is ToolMode.MOCK


@pytest.mark.asyncio
async def test_real_mode_still_refuses_a_side_effecting_tool() -> None:
    # "real" is a blanket default, not consent for every destructive tool.
    gw = _gateway(policy=ToolPolicy(default_mode=ToolMode.REAL))

    out = await gw.call("scm_merge_pull_request", {"number": 7}, _real)

    assert "blocked by sandbox" in out
    assert gw.records[0].mode is ToolMode.BLOCK


@pytest.mark.asyncio
async def test_a_side_effecting_tool_runs_when_explicitly_opted_in() -> None:
    gw = _gateway(policy=ToolPolicy(default_mode=ToolMode.MOCK, overrides={"scm_merge_pull_request": ToolMode.REAL}))

    out = await gw.call("scm_merge_pull_request", {"number": 7}, _real)

    assert out == "did the thing"
    assert gw.records[0].mode is ToolMode.REAL


@pytest.mark.asyncio
async def test_a_read_only_tool_runs_for_real_under_the_real_default() -> None:
    gw = _gateway(policy=ToolPolicy(default_mode=ToolMode.REAL))

    assert await gw.call("scm_read_file", {"path": "x"}, _real) == "did the thing"


# ── block ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_blocked_call_returns_a_structured_result_not_a_crash() -> None:
    gw = _gateway(policy=ToolPolicy(default_mode=ToolMode.BLOCK))

    out = await gw.call("scm_commit_file", {"path": "a"}, _real)

    payload = json.loads(out)
    assert payload["blocked_by_sandbox"] is True
    assert payload["tool"] == "scm_commit_file"
    assert payload["reason"]


@pytest.mark.asyncio
async def test_a_blocked_call_never_reaches_the_real_tool() -> None:
    calls: list[int] = []

    async def real() -> str:
        calls.append(1)
        return "boom"

    await _gateway(policy=ToolPolicy(default_mode=ToolMode.BLOCK)).call("scm_commit_file", {}, real)

    assert calls == []


# ── mock ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_fixture_supplies_the_mock_response() -> None:
    gw = _gateway(fixtures={"scm_read_file": "# hello"})

    assert await gw.call("scm_read_file", {"path": "README.md"}, _real) == "# hello"


@pytest.mark.asyncio
async def test_a_missing_fixture_still_answers_deterministically() -> None:
    gw = _gateway()

    first = await gw.call("scm_read_file", {"path": "a"}, _real)
    second = await gw.call("scm_read_file", {"path": "a"}, _real)

    assert first == second


@pytest.mark.asyncio
async def test_a_callable_fixture_sees_the_arguments() -> None:
    gw = _gateway(fixtures={"scm_read_file": lambda args: f"contents of {args['path']}"})

    assert await gw.call("scm_read_file", {"path": "README.md"}, _real) == "contents of README.md"


# ── replay ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_replay_returns_the_recorded_response_for_the_same_arguments() -> None:
    recorded = _gateway(policy=ToolPolicy(default_mode=ToolMode.REAL))
    await recorded.call("scm_read_file", {"path": "a"}, lambda: _real("v1"))

    gw = _gateway(policy=ToolPolicy(default_mode=ToolMode.REPLAY), recording=recorded.recording())

    assert await gw.call("scm_read_file", {"path": "a"}, _real) == "v1"


@pytest.mark.asyncio
async def test_replay_is_free_and_never_calls_the_real_tool() -> None:
    calls: list[int] = []

    async def real() -> str:
        calls.append(1)
        return "live"

    gw = _gateway(
        policy=ToolPolicy(default_mode=ToolMode.REPLAY),
        recording={"scm_read_file": [{"arguments": {"path": "a"}, "response": "v1"}]},
    )
    await gw.call("scm_read_file", {"path": "a"}, real)

    assert calls == []


@pytest.mark.asyncio
async def test_a_replay_miss_is_explicit_rather_than_a_silent_live_call() -> None:
    gw = _gateway(policy=ToolPolicy(default_mode=ToolMode.REPLAY))

    out = await gw.call("scm_read_file", {"path": "unseen"}, _real)

    assert "no recorded response" in out
    assert gw.records[0].error


# ── the record is the deliverable ─────────────────────────────────────


@pytest.mark.asyncio
async def test_every_call_is_recorded_with_what_a_scorer_needs() -> None:
    gw = _gateway(policy=ToolPolicy(default_mode=ToolMode.REAL))

    await gw.call("scm_read_file", {"path": "a"}, _real)
    rec: ToolCallRecord = gw.records[0]

    assert rec.tool == "scm_read_file"
    assert rec.arguments == {"path": "a"}
    assert rec.response == "did the thing"
    assert rec.mode is ToolMode.REAL
    assert rec.latency_ms >= 0
    assert rec.error is None
    assert rec.blocked is False


@pytest.mark.asyncio
async def test_calls_are_recorded_in_order_so_a_trajectory_can_be_scored() -> None:
    gw = _gateway()

    await gw.call("scm_read_file", {}, _real)
    await gw.call("scm_list_files", {}, _real)

    assert [r.tool for r in gw.records] == ["scm_read_file", "scm_list_files"]


@pytest.mark.asyncio
async def test_a_failing_real_tool_is_recorded_not_raised() -> None:
    async def real() -> str:
        raise RuntimeError("upstream 500")

    gw = _gateway(policy=ToolPolicy(default_mode=ToolMode.REAL))
    out = await gw.call("scm_read_file", {}, real)

    assert "upstream 500" in out
    assert "upstream 500" in (gw.records[0].error or "")


@pytest.mark.asyncio
async def test_secrets_are_redacted_out_of_the_record() -> None:
    gw = _gateway(policy=ToolPolicy(default_mode=ToolMode.REAL))

    await gw.call("scm_read_file", {"token": "ghp_abcdefghijklmnopqrstuvwxyz0123456789"}, _real)

    assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789" not in json.dumps(gw.records[0].arguments)


@pytest.mark.asyncio
async def test_records_stream_to_a_sink_as_they_happen() -> None:
    seen: list[ToolCallRecord] = []
    gw = _gateway(sink=seen.append)

    await gw.call("scm_read_file", {}, _real)

    assert len(seen) == 1


# ── building one from a sandbox pod's environment ─────────────────────


def test_a_gateway_is_built_from_the_sandbox_env() -> None:
    env = {
        "DEVAI_SANDBOX_TOOL_MODE": "block",
        "DEVAI_SANDBOX_TOOL_OVERRIDES": '{"scm_read_file": "real"}',
    }

    gw = ToolGateway.from_env(env)

    assert gw.policy.default_mode is ToolMode.BLOCK
    assert gw.policy.mode_for("scm_read_file") is ToolMode.REAL


def test_no_sandbox_env_means_no_gateway_so_production_is_unaffected() -> None:
    assert ToolGateway.from_env({}) is None


def test_a_nonsense_mode_in_the_env_falls_back_to_blocking() -> None:
    gw = ToolGateway.from_env({"DEVAI_SANDBOX_TOOL_MODE": "yolo"})

    assert gw.policy.default_mode is ToolMode.BLOCK


# ── dispatcher integration ────────────────────────────────────────────


class _Executor:
    def __init__(self, scm: Any = None) -> None:
        self.calls: list[str] = []

    async def execute(self, name: str, arguments: dict[str, Any]) -> str:
        self.calls.append(name)
        return "live result"


def _dispatcher(gateway: ToolGateway | None) -> Any:
    from devai.tools.dispatch import ToolDispatcher

    d = ToolDispatcher(gateway=gateway)
    d._index["scm_merge_pull_request"] = ({}, "fake.module")  # noqa: SLF001
    d._index["scm_read_file"] = ({}, "fake.module")  # noqa: SLF001
    d._executors["fake.module"] = _Executor()  # noqa: SLF001
    return d


@pytest.mark.asyncio
async def test_a_sandboxed_dispatcher_blocks_a_destructive_call() -> None:
    gw = ToolGateway(policy=ToolPolicy(default_mode=ToolMode.REAL))
    d = _dispatcher(gw)

    out = await d.execute("scm_merge_pull_request", {"number": 1})

    assert json.loads(out)["blocked_by_sandbox"] is True
    assert d._executors["fake.module"].calls == []  # noqa: SLF001
    assert gw.records[0].blocked is True


@pytest.mark.asyncio
async def test_the_same_agent_in_production_is_unaffected() -> None:
    d = _dispatcher(None)

    assert await d.execute("scm_merge_pull_request", {"number": 1}) == "live result"


@pytest.mark.asyncio
async def test_the_allowlist_still_wins_over_the_gateway() -> None:
    d = _dispatcher(ToolGateway(policy=ToolPolicy(default_mode=ToolMode.REAL)))
    d._allowed = {"scm_read_file"}  # noqa: SLF001 — what build_tool_specs records

    out = await d.execute("scm_merge_pull_request", {"number": 1})

    assert "not permitted" in out


# ── MCP tools obey the same policy ────────────────────────────────────


class _FakeHub:
    """Minimal stand-in for MCPHub's call path."""

    def __init__(self, gateway: ToolGateway | None) -> None:
        self._gateway = gateway
        self.reached: list[str] = []

    async def _call_downstream(self, name: str, arguments: dict[str, Any]) -> Any:
        self.reached.append(name)
        return {"ok": True}


@pytest.mark.asyncio
async def test_an_mcp_tool_is_blocked_by_the_same_policy() -> None:
    from devai.sandbox.gateway import guard_mcp_call

    hub = _FakeHub(ToolGateway(policy=ToolPolicy(default_mode=ToolMode.BLOCK)))
    out = await guard_mcp_call(
        hub._gateway, "acme__refund_customer", {"id": 1}, lambda: hub._call_downstream("acme__refund_customer", {})
    )

    assert json.loads(out)["blocked_by_sandbox"] is True
    assert hub.reached == []


@pytest.mark.asyncio
async def test_an_mcp_tool_outside_a_sandbox_is_untouched() -> None:
    from devai.sandbox.gateway import guard_mcp_call

    hub = _FakeHub(None)
    out = await guard_mcp_call(
        None, "acme__refund_customer", {}, lambda: hub._call_downstream("acme__refund_customer", {})
    )

    assert out == {"ok": True}
    assert hub.reached == ["acme__refund_customer"]


@pytest.mark.asyncio
async def test_the_hub_blocks_a_sandboxed_mcp_call_before_it_leaves_the_pod(monkeypatch: Any) -> None:
    from devai.mcphub.hub import MCPHub

    monkeypatch.setenv("DEVAI_SANDBOX_TOOL_MODE", "block")
    hub = MCPHub(registry=None)  # type: ignore[arg-type]

    reached: list[str] = []

    def _leg(server: str, name: str) -> Any:
        reached.append(name)
        raise AssertionError("downstream must not be reached")

    monkeypatch.setattr(hub, "_healthy_leg", _leg)

    out = await hub.call_tool("acme__refund_customer", {"id": 1})

    assert json.loads(out)["blocked_by_sandbox"] is True
    assert reached == []
