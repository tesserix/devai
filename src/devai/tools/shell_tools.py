"""Terminal execution tool — the "runs terminal commands" Cursor capability.

`shell_exec` runs a command in the task's sandbox working tree
(`ToolContext.workdir`, set by the runner to a per-task git worktree inside
the runner pod). It is intentionally inert outside a sandbox: with no
workdir set it refuses, so an agent running in the orchestrator process
can't shell out against the host.

Output lines are also emitted on stdout as `LOG::{json}` line-protocol so
the runner pod's log tail (`devai.runtime.job_watcher`) can stream the
terminal to the dashboard live. The same protocol carries CHECKPOINT:: and
RESULT:: (see checkpoint_tools / the runner entrypoint).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shlex
import time
from typing import Any

from devai.tools.registry import Handler, ToolContext, register

_SCHEMA = {
    "type": "object",
    "properties": {
        "command": {"type": "string", "description": "Shell command to run in the project working tree"},
        "cwd": {"type": "string", "description": "Optional sub-directory (relative to the working tree)"},
        "timeout": {"type": "integer", "description": "Max seconds before the command is killed (default 300)"},
    },
    "required": ["command"],
}

_MAX_OUTPUT = 16000  # chars returned to the model
_DEFAULT_TIMEOUT = 300


def emit_log(stream: str, line: str) -> None:
    """Print one LOG:: line-protocol record for the pod log tail."""
    with contextlib.suppress(Exception):  # logging must never break the tool
        print("LOG::" + json.dumps({"stream": stream, "line": line, "ts": time.time()}), flush=True)


async def run_command(command: str, *, workdir: str, cwd: str = "", timeout: int = _DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Run a shell command in `workdir` (optionally a subdir), capped by timeout."""
    import os

    run_dir = os.path.join(workdir, cwd) if cwd else workdir
    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=run_dir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return {"exit_code": 124, "output": f"(command timed out after {timeout}s)", "timed_out": True}
    output = (out_bytes or b"").decode("utf-8", errors="replace")
    for line in output.splitlines():
        emit_log("stdout", line)
    return {"exit_code": proc.returncode, "output": output, "timed_out": False}


def _shell_factory(ctx: ToolContext) -> Handler:
    async def handler(args: dict[str, Any]) -> str:
        if not ctx.workdir:
            return "ERROR: no sandbox working tree available — shell_exec only runs inside the runner pod"
        command = (args.get("command") or "").strip()
        if not command:
            return "ERROR: command is required"
        cwd = args.get("cwd") or ""
        timeout = int(args.get("timeout") or _DEFAULT_TIMEOUT)
        # Light guardrail: reject path-escape via cwd
        if cwd and (cwd.startswith("/") or ".." in shlex.split(cwd if " " not in cwd else cwd)):
            return "ERROR: cwd must be a relative path inside the working tree"
        try:
            result = await run_command(command, workdir=ctx.workdir, cwd=cwd, timeout=timeout)
        except Exception as e:  # noqa: BLE001
            return f"ERROR running command: {e}"
        output = result["output"]
        if len(output) > _MAX_OUTPUT:
            output = output[:_MAX_OUTPUT] + "\n… (output truncated)"
        return f"exit_code={result['exit_code']}\n{output}"

    return handler


def register_shell_tools(*, overwrite: bool = False) -> None:
    register(
        "shell_exec",
        "Run a shell command (build, test, install, git, …) in the project's sandbox working tree "
        "and return its combined output and exit code.",
        _SCHEMA,
        _shell_factory,
        overwrite=overwrite,
    )


register_shell_tools()

__all__ = ["emit_log", "register_shell_tools", "run_command"]
