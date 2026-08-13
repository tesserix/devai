"""The tool gateway — what a tool call actually reaches inside a sandbox.

Isolating a process is not enough for an agent: it decides at runtime which
tools to call, so what has to be isolated is the *side effect*. Every call goes
through one chokepoint that resolves a mode (real / mock / replay / block),
produces a result the model can read either way, and records what happened.

The record is the deliverable — a score is only an index into it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from devai.sandbox.models import ToolMode, ToolPolicy
from devai.services.redact import redact_secrets

logger = logging.getLogger(__name__)

RealCall = Callable[[], Awaitable[Any]]

# Read-only tools worth naming explicitly: everything unclassified is treated as
# side-effecting, so this list only has to cover the safe exceptions we ship.
_READ_ONLY_PREFIXES = (
    "scm_read",
    "scm_list",
    "scm_get",
    "scm_search",
    "kubectl_get",
    "kubectl_describe",
    "kubectl_logs",
    "prometheus_",
    "read_",
    "list_",
    "get_",
    "search_",
    "analyze_",
    "detect_",
    "validate_",
    "recall_",
    "web_search",
)


def is_side_effecting(tool: str) -> bool:
    """Whether a call to ``tool`` can change the world outside this run.

    Unknown tools count as side-effecting. An MCP tool nobody classified may
    well be ``refund_customer``, and the cost of guessing wrong in the other
    direction is unbounded.
    """
    from devai.tools.dispatch import MUTATING_TOOLS

    if tool in MUTATING_TOOLS:
        return True
    return not tool.startswith(_READ_ONLY_PREFIXES)


@dataclass(slots=True)
class ToolCallRecord:
    """One tool call, as it actually happened."""

    tool: str
    arguments: dict[str, Any]
    mode: ToolMode
    response: str = ""
    error: str | None = None
    latency_ms: int = 0
    blocked: bool = False
    started_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "arguments": self.arguments,
            "mode": self.mode.value,
            "response": self.response,
            "error": self.error,
            "latency_ms": self.latency_ms,
            "blocked": self.blocked,
            "started_at": self.started_at,
        }


class ToolGateway:
    """Applies a :class:`ToolPolicy` to every tool call in a sandbox."""

    def __init__(
        self,
        *,
        policy: ToolPolicy | None = None,
        fixtures: Mapping[str, Any] | None = None,
        recording: Mapping[str, list[dict[str, Any]]] | None = None,
        sink: Callable[[ToolCallRecord], None] | None = None,
    ) -> None:
        self.policy = policy or ToolPolicy()
        self._fixtures = dict(fixtures or {})
        self._recording = {k: list(v) for k, v in (recording or {}).items()}
        self._sink = sink
        self.records: list[ToolCallRecord] = []

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> ToolGateway | None:
        """Build the gateway a sandbox pod was launched with, or None.

        None means "not in a sandbox" — production dispatch is untouched.
        """
        raw_mode = env.get("DEVAI_SANDBOX_TOOL_MODE")
        if not raw_mode:
            return None
        overrides: dict[str, ToolMode] = {}
        for tool, mode in _load_json(env.get("DEVAI_SANDBOX_TOOL_OVERRIDES")).items():
            parsed = _parse_mode(mode)
            if parsed is not None:
                overrides[tool] = parsed
        return cls(policy=ToolPolicy(default_mode=_parse_mode(raw_mode) or ToolMode.BLOCK, overrides=overrides))

    def mode_for(self, tool: str) -> ToolMode:
        """Resolve the mode, refusing to run an unrequested side effect for real.

        ``real`` as a *default* is a blanket setting, not consent to merge a PR
        or scale a deployment; only a per-tool override is.
        """
        explicit = self.policy.overrides.get(tool)
        if explicit is not None:
            return explicit
        if self.policy.default_mode is ToolMode.REAL and is_side_effecting(tool):
            return ToolMode.BLOCK
        return self.policy.default_mode

    async def call(self, tool: str, arguments: dict[str, Any], real: RealCall) -> str:
        """Run one tool call under the policy. Always returns model-readable text."""
        mode = self.mode_for(tool)
        record = ToolCallRecord(tool=tool, arguments=_redact_args(arguments), mode=mode)
        started = time.perf_counter()
        try:
            if mode is ToolMode.BLOCK:
                record.blocked = True
                response = _blocked_result(tool, "tool blocked by sandbox policy")
            elif mode is ToolMode.MOCK:
                response = self._mock(tool, arguments)
            elif mode is ToolMode.REPLAY:
                response = self._replay(tool, arguments, record)
            else:
                response = _as_text(await real())
                self._remember(tool, arguments, response)
        except Exception as e:  # noqa: BLE001 — a tool failure is a result, not a crash
            record.error = str(e)
            response = f"error: tool {tool} failed: {e}"
        record.latency_ms = int((time.perf_counter() - started) * 1000)
        record.response = redact_secrets(response)
        self.records.append(record)
        if record.blocked or record.mode is ToolMode.REAL:
            logger.info("sandbox tool %s mode=%s blocked=%s", tool, mode.value, record.blocked)
        if self._sink is not None:
            self._sink(record)
        return response

    def recording(self) -> dict[str, list[dict[str, Any]]]:
        """The real calls made so far, in a shape :meth:`from_env` replay can read."""
        return {k: list(v) for k, v in self._recording.items()}

    # ── modes ─────────────────────────────────────────────────────────

    def _mock(self, tool: str, arguments: dict[str, Any]) -> str:
        fixture = self._fixtures.get(tool)
        if callable(fixture):
            return _as_text(fixture(arguments))
        if fixture is not None:
            return _as_text(fixture)
        # No fixture: answer deterministically so a re-run of the same suite
        # produces the same trajectory instead of drifting.
        return f"[mock] {tool} was not executed (sandbox mock mode). args_digest={_digest(arguments)}"

    def _replay(self, tool: str, arguments: dict[str, Any], record: ToolCallRecord) -> str:
        digest = _digest(arguments)
        for entry in self._recording.get(tool, []):
            if _digest(entry.get("arguments") or {}) == digest:
                return _as_text(entry.get("response", ""))
        record.error = f"no recorded response for {tool}"
        return f"error: no recorded response for {tool} with these arguments (replay mode)"

    def _remember(self, tool: str, arguments: dict[str, Any], response: str) -> None:
        self._recording.setdefault(tool, []).append({"arguments": arguments, "response": response})


async def guard_mcp_call(gateway: ToolGateway | None, tool: str, arguments: dict[str, Any], real: RealCall) -> Any:
    """Put an MCP tool call under the same policy as a built-in one.

    Outside a sandbox ``gateway`` is None and the downstream result passes
    through untouched, shape and all.
    """
    if gateway is None:
        return await real()
    return await gateway.call(tool, arguments or {}, real)


def _blocked_result(tool: str, reason: str) -> str:
    """Structured, so the model can reason about the refusal and move on."""
    return json.dumps({"blocked_by_sandbox": True, "tool": tool, "reason": reason})


def _digest(arguments: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(arguments, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _redact_args(arguments: dict[str, Any] | None) -> dict[str, Any]:
    cleaned: dict[str, Any] = json.loads(redact_secrets(json.dumps(arguments or {}, default=str)))
    return cleaned


def _as_text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, default=str)


def _parse_mode(value: Any) -> ToolMode | None:
    try:
        return ToolMode(str(value).lower())
    except ValueError:
        return None


def _load_json(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


__all__ = ["ToolCallRecord", "ToolGateway", "guard_mcp_call", "is_side_effecting"]
