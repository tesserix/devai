"""Tests for the runner pod line-protocol parser + checkpoint serialization."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from devai.adapters.telemetry.noop import NoopTelemetryAdapter
from devai.adapters.telemetry.runtime import get_global_telemetry
from devai.pipeline.types import DevAITask, StageEvent, StageEventPhase
from devai.runner import entrypoint
from devai.runtime.protocol import parse_runner_line, parse_runner_lines


def test_parse_mixed_stdout():
    blob = "\n".join(
        [
            "Cloning repo...",
            'LOG::{"stream": "stdout", "line": "npm install", "ts": 1.0}',
            "some noise that is not protocol",
            'CHECKPOINT::{"sha": "abc123", "label": "scaffold", "ts": 2.0}',
            'LOG::{"stream": "stdout", "line": "build ok", "ts": 3.0}',
            'CHECKPOINT::{"sha": "def456", "label": "feature", "ts": 4.0}',
            'RESULT::{"pr_number": 42, "summary": "done"}',
        ]
    )
    parsed = parse_runner_lines(blob)

    assert len(parsed.logs) == 2
    assert parsed.logs[0]["line"] == "npm install"
    assert [c["sha"] for c in parsed.checkpoints] == ["abc123", "def456"]
    assert parsed.result == {"pr_number": 42, "summary": "done"}
    assert "Cloning repo..." in parsed.plain_lines
    assert "some noise that is not protocol" in parsed.plain_lines


def test_last_result_wins():
    blob = 'RESULT::{"v": 1}\nRESULT::{"v": 2}'
    assert parse_runner_lines(blob).result == {"v": 2}


def test_malformed_payload_is_plain():
    assert parse_runner_line("CHECKPOINT::{not json}") is None
    parsed = parse_runner_lines("CHECKPOINT::{not json}")
    assert parsed.checkpoints == []
    assert parsed.plain_lines == ["CHECKPOINT::{not json}"]


def test_parse_single_line_kinds():
    assert parse_runner_line('LOG::{"line":"x"}')[0] == "log"
    assert parse_runner_line('CHECKPOINT::{"sha":"y"}')[0] == "checkpoint"
    assert parse_runner_line('RESULT::{"z":1}')[0] == "result"
    assert parse_runner_line("plain text") is None


def test_stage_event_serializes_checkpoint():
    ev = StageEvent(stage="implement", phase=StageEventPhase.COMPLETED, checkpoint="abc123")
    assert ev.to_dict()["checkpoint"] == "abc123"


def test_task_records_checkpoint_and_serializes():
    task = DevAITask(intent="x", current_stage="implementing")
    entry = task.record_checkpoint("abc123", label="scaffold")
    assert entry["sha"] == "abc123"
    assert entry["stage"] == "implementing"
    dumped = json.dumps(task.to_dict())  # must be JSON-serializable
    assert "checkpoints" in json.loads(dumped)
    assert json.loads(dumped)["checkpoints"][0]["sha"] == "abc123"


class ClosingTelemetry(NoopTelemetryAdapter):
    provider_name = "recording"

    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_runner_installs_and_flushes_configured_telemetry(monkeypatch, capsys):
    sink = ClosingTelemetry()
    observed_provider = ""

    async def resolve_agent(_agent_name, _config):
        return None

    async def handler(**_kwargs):
        nonlocal observed_provider
        observed_provider = get_global_telemetry().provider_name
        return {"ok": True}

    monkeypatch.setenv("DEVAI_RUNNER_TASK_ID", "run-1")
    monkeypatch.setenv("DEVAI_RUNNER_STAGE", "trace_stage")
    monkeypatch.setenv("DEVAI_RUNNER_AGENT", "trace_agent")
    monkeypatch.setattr(entrypoint, "_load_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(entrypoint, "_decode_agent_profile_from_env", lambda: None)
    monkeypatch.setattr(entrypoint, "_resolve_agent", resolve_agent)
    monkeypatch.setitem(entrypoint._STAGE_HANDLERS, "trace_agent", handler)
    monkeypatch.setattr("devai.adapters.telemetry.create_telemetry_adapter", lambda _config: sink)

    assert await entrypoint._run() == 0

    assert observed_provider == "recording"
    assert sink.closed
    assert get_global_telemetry().provider_name == "noop"
    assert 'RESULT::{"ok": true}' in capsys.readouterr().out


@pytest.mark.asyncio
async def test_runner_flushes_telemetry_when_agent_resolution_fails(monkeypatch, capsys):
    sink = ClosingTelemetry()

    async def fail_resolution(_agent_name, _config):
        raise RuntimeError("registry unavailable")

    monkeypatch.setenv("DEVAI_RUNNER_TASK_ID", "run-2")
    monkeypatch.setenv("DEVAI_RUNNER_STAGE", "trace_stage")
    monkeypatch.setenv("DEVAI_RUNNER_AGENT", "trace_agent")
    monkeypatch.setattr(entrypoint, "_load_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(entrypoint, "_decode_agent_profile_from_env", lambda: None)
    monkeypatch.setattr(entrypoint, "_resolve_agent", fail_resolution)
    monkeypatch.setattr("devai.adapters.telemetry.create_telemetry_adapter", lambda _config: sink)

    assert await entrypoint._run() == 1

    assert sink.closed
    assert get_global_telemetry().provider_name == "noop"
    assert 'RESULT::{"ok": false' in capsys.readouterr().out
