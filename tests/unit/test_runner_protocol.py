"""Tests for the runner pod line-protocol parser + checkpoint serialization."""

from __future__ import annotations

import json

from devai.pipeline.types import DevAITask, StageEvent, StageEventPhase
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
