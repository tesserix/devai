"""Agent-facing memory tools.

The reflector specialization (and any YAML role that declares them) closes
the learning loop with `agent_memory_remember` / `agent_memory_recall` —
these existed only as names in YAML until now, so the reflector ran
tool-less. Backed by the process-global memory adapter (pgvector in prod,
noop in tests), so every role shares the same configured store.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

MEMORY_TOOLS: list[dict[str, Any]] = [
    {
        "name": "agent_memory_remember",
        "description": (
            "Persist a learning for future runs: a pattern that worked, a pitfall to avoid, "
            "or a repo convention. Be specific — future agents recall these by semantic search."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "The learning, 1-3 concrete sentences"},
                "repo": {"type": "string", "description": "org/repo this applies to ('global' if universal)"},
                "memory_type": {
                    "type": "string",
                    "enum": ["episodic", "semantic", "procedural"],
                    "description": "episodic=what happened, semantic=pattern/convention, procedural=how-to",
                },
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["content"],
        },
    },
    {
        "name": "agent_memory_recall",
        "description": "Recall relevant learnings from past runs by free-text query (semantic search).",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What you want to know"},
                "repo": {"type": "string", "description": "Restrict to one org/repo (optional)"},
                "limit": {"type": "integer", "description": "Max results (default 5)"},
            },
            "required": ["query"],
        },
    },
]


class MemoryToolExecutor:
    """Executes agent memory tools against the process-global adapter."""

    def __init__(self, agent_name: Any = "") -> None:
        # The ToolDispatcher constructs executors as cls(scm) with a fallback
        # to cls() — tolerate being handed a client object in that slot.
        self.agent_name = agent_name if isinstance(agent_name, str) else ""

    async def execute(self, tool_name: str, tool_input: dict[str, Any]) -> str:
        handler = getattr(self, f"_handle_{tool_name}", None)
        if handler is None:
            return f"Unknown tool: {tool_name}"
        try:
            result = await handler(tool_input)
        except Exception as e:  # noqa: BLE001 — memory must never crash an agent
            logger.exception("memory tool %s failed", tool_name)
            return f"memory unavailable: {e}"
        return json.dumps(result, indent=2, default=str)

    async def _handle_agent_memory_remember(self, inp: dict[str, Any]) -> dict[str, Any]:
        from devai.adapters.memory.runtime import get_global_memory

        record = await get_global_memory().remember(
            str(inp.get("content") or "")[:2000],
            agent=self.agent_name or "reflector",
            repo=str(inp.get("repo") or "global"),
            memory_type=str(inp.get("memory_type") or "semantic"),
            tags=[t for t in (inp.get("tags") or []) if isinstance(t, str)][:8],
        )
        return {"stored": True, "id": getattr(record, "id", ""), "provider": getattr(record, "provider", "")}

    async def _handle_agent_memory_recall(self, inp: dict[str, Any]) -> dict[str, Any]:
        from devai.adapters.memory.runtime import get_global_memory

        records = await get_global_memory().recall(
            query=str(inp.get("query") or "")[:500],
            repo=str(inp["repo"]) if inp.get("repo") else None,
            limit=min(int(inp.get("limit") or 5), 20),
        )
        return {
            "count": len(records),
            "memories": [
                {
                    "content": getattr(r, "content", ""),
                    "type": str(getattr(r, "memory_type", "")),
                    "agent": getattr(r, "agent", ""),
                    "repo": getattr(r, "repo", ""),
                }
                for r in records
            ],
        }


__all__ = ["MEMORY_TOOLS", "MemoryToolExecutor"]
