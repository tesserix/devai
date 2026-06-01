"""Runner pod line-protocol — the contract between the runner and the api.

The `devai-runner` pod (and the in-pod AgentRunner / shell + checkpoint
tools) emit structured records on stdout, one per line, so the api pod's
`JobWatcher` can stream the terminal + checkpoints to the dashboard and
recover the stage's structured result without a side channel:

    LOG::{"stream":"stdout","line":"...","ts":...}     # one terminal line
    CHECKPOINT::{"sha":"...","label":"...","ts":...}    # a git restore point
    RESULT::{...}                                        # the stage handover (final)

Everything else on stdout is treated as plain log output. `parse_runner_lines`
is pure (no I/O) so it's unit-testable against captured fixtures and reused
by both the batch path (parse final logs) and the live-tail path (parse each
line as it arrives).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

LOG_PREFIX = "LOG::"
CHECKPOINT_PREFIX = "CHECKPOINT::"
RESULT_PREFIX = "RESULT::"


@dataclass(slots=True)
class ParsedRunnerOutput:
    """Structured view of a runner pod's stdout."""

    logs: list[dict[str, Any]] = field(default_factory=list)  # {stream, line, ts}
    checkpoints: list[dict[str, Any]] = field(default_factory=list)  # {sha, label, ts}
    result: dict[str, Any] | None = None  # the last RESULT:: payload wins
    plain_lines: list[str] = field(default_factory=list)  # non-protocol stdout


def parse_runner_line(line: str) -> tuple[str, Any] | None:
    """Parse ONE line. Returns (kind, payload) or None for plain output.

    kind ∈ {"log", "checkpoint", "result"}. A malformed JSON payload after a
    known prefix is treated as plain output rather than raising.
    """
    stripped = line.rstrip("\n")
    for prefix, kind in ((LOG_PREFIX, "log"), (CHECKPOINT_PREFIX, "checkpoint"), (RESULT_PREFIX, "result")):
        if stripped.startswith(prefix):
            raw = stripped[len(prefix) :].strip()
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                return None
            return kind, payload
    return None


def parse_runner_lines(text: str) -> ParsedRunnerOutput:
    """Parse a whole stdout blob into a ParsedRunnerOutput."""
    out = ParsedRunnerOutput()
    for line in text.splitlines():
        parsed = parse_runner_line(line)
        if parsed is None:
            if line.strip():
                out.plain_lines.append(line)
            continue
        kind, payload = parsed
        if kind == "log" and isinstance(payload, dict):
            out.logs.append(payload)
        elif kind == "checkpoint" and isinstance(payload, dict):
            out.checkpoints.append(payload)
        elif kind == "result" and isinstance(payload, dict):
            out.result = payload  # last one wins
    return out


__all__ = [
    "CHECKPOINT_PREFIX",
    "LOG_PREFIX",
    "RESULT_PREFIX",
    "ParsedRunnerOutput",
    "parse_runner_line",
    "parse_runner_lines",
]
