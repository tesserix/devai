"""Git checkpoint + rollback tools — the "Git & checkpoints" Cursor capability.

`checkpoint(label)` commits the current sandbox working tree and returns the
commit SHA — that SHA is a restore point. `rollback(sha)` hard-resets the
working tree to a prior checkpoint (self-repair, or a user-driven undo from
the dashboard timeline).

Each checkpoint also prints a `CHECKPOINT::{json}` line-protocol record on
stdout so the runner pod's log tail surfaces it to the dashboard's
checkpoint timeline in real time. Both tools require a git working tree
(`ToolContext.workdir`); outside the sandbox they refuse.
"""

from __future__ import annotations

import contextlib
import json
import time
from typing import Any

from devai.tools.registry import Handler, ToolContext, register
from devai.tools.shell_tools import run_command

_CHECKPOINT_SCHEMA = {
    "type": "object",
    "properties": {
        "label": {"type": "string", "description": "Short human-readable name for this checkpoint"},
    },
    "required": ["label"],
}

_ROLLBACK_SCHEMA = {
    "type": "object",
    "properties": {
        "sha": {"type": "string", "description": "The checkpoint commit SHA to reset the working tree to"},
    },
    "required": ["sha"],
}


def emit_checkpoint(sha: str, label: str) -> None:
    with contextlib.suppress(Exception):
        print("CHECKPOINT::" + json.dumps({"sha": sha, "label": label, "ts": time.time()}), flush=True)


def _checkpoint_factory(ctx: ToolContext) -> Handler:
    async def handler(args: dict[str, Any]) -> str:
        if not ctx.workdir:
            return "ERROR: no sandbox working tree — checkpoints only exist inside the runner pod"
        label = (args.get("label") or "checkpoint").strip()
        # Stage everything and commit. --allow-empty so a no-op step still
        # produces a restore point; -q to keep output terse.
        add = await run_command("git add -A", workdir=ctx.workdir, timeout=60)
        if add["exit_code"] != 0:
            return f"ERROR: git add failed:\n{add['output']}"
        msg = label.replace('"', "'")
        commit = await run_command(
            f'git commit --allow-empty -q -m "checkpoint: {msg}"', workdir=ctx.workdir, timeout=60
        )
        if commit["exit_code"] != 0:
            return f"ERROR: git commit failed:\n{commit['output']}"
        rev = await run_command("git rev-parse HEAD", workdir=ctx.workdir, timeout=30)
        sha = (rev["output"] or "").strip()
        emit_checkpoint(sha, label)
        return f"checkpoint created: {sha[:12]} ({label})"

    return handler


def _rollback_factory(ctx: ToolContext) -> Handler:
    async def handler(args: dict[str, Any]) -> str:
        if not ctx.workdir:
            return "ERROR: no sandbox working tree — rollback only works inside the runner pod"
        sha = (args.get("sha") or "").strip()
        if not sha:
            return "ERROR: sha is required"
        # Validate it looks like a git rev to avoid arg injection
        if not all(c.isalnum() for c in sha):
            return "ERROR: sha must be an alphanumeric git revision"
        reset = await run_command(f"git reset --hard {sha}", workdir=ctx.workdir, timeout=60)
        if reset["exit_code"] != 0:
            return f"ERROR: git reset failed:\n{reset['output']}"
        return f"rolled back working tree to {sha[:12]}"

    return handler


def register_checkpoint_tools(*, overwrite: bool = False) -> None:
    register(
        "checkpoint",
        "Commit the current working tree as a named restore point and return its commit SHA.",
        _CHECKPOINT_SCHEMA,
        _checkpoint_factory,
        overwrite=overwrite,
    )
    register(
        "rollback",
        "Reset the working tree back to a previous checkpoint commit SHA.",
        _ROLLBACK_SCHEMA,
        _rollback_factory,
        overwrite=overwrite,
    )


register_checkpoint_tools()

__all__ = ["emit_checkpoint", "register_checkpoint_tools"]
