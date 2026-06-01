"""Resolve tool names to LLM tool declarations and execute them.

Bridges the built-in tool catalog (``tools/*.py``) to the tool-calling
loop the YAML specialization runner drives. A specialization lists tool
names in ``allowed_tools``; this dispatcher turns those into
:class:`ToolSpec` declarations for the LLM request and routes the model's
tool calls back to the right executor.

Executors are discovered, not hard-coded: each ``tools/<x>.py`` module
exposes a ``*_TOOLS`` list (name + ``input_schema``) and a
``*ToolExecutor`` class with ``async execute(name, input) -> str | dict``.
"""

from __future__ import annotations

import importlib
import json
import logging
from typing import Any

from devai.adapters.llm.base import ToolSpec

logger = logging.getLogger(__name__)

# (category, module, TOOLS attribute). Executor class is found by suffix.
_SOURCES = (
    ("scm", "devai.tools.scm_tools", "SCM_TOOLS"),
    ("validation", "devai.tools.validation_tools", "VALIDATION_TOOLS"),
    ("document", "devai.tools.document_tools", "DOCUMENT_TOOLS"),
    ("security", "devai.tools.security_tools", "SECURITY_TOOLS"),
    ("test", "devai.tools.test_tools", "TEST_TOOLS"),
    ("file", "devai.tools.file_tools", "FILE_TOOLS"),
)


class ToolDispatcher:
    """Maps tool names → declarations + executes calls for one task.

    Construct per stage run. ``scm`` is threaded into executors whose
    constructor accepts it (SCM / security / test / validation tools);
    others are built with no args.
    """

    def __init__(self, scm: Any | None = None) -> None:
        self._scm = scm
        # name → (schema dict, module path)
        self._index: dict[str, tuple[dict[str, Any], str]] = {}
        # module path → executor instance (lazily constructed, cached)
        self._executors: dict[str, Any] = {}
        self._build_index()

    def _build_index(self) -> None:
        for _category, module_path, attr in _SOURCES:
            try:
                mod = importlib.import_module(module_path)
            except Exception:  # noqa: BLE001 — a missing tool module shouldn't kill the run
                logger.debug("tool source %s not importable — skipped", module_path, exc_info=True)
                continue
            tools = getattr(mod, attr, None)
            if not isinstance(tools, list):
                continue
            for t in tools:
                if isinstance(t, dict) and t.get("name"):
                    self._index[t["name"]] = (t.get("input_schema") or {}, module_path)

    def build_tool_specs(self, names: list[str]) -> list[ToolSpec]:
        """ToolSpec list for the names a specialization allows.

        Unknown names are skipped with a warning rather than failing the
        run — a typo'd tool just won't be offered to the model.
        """
        specs: list[ToolSpec] = []
        for name in names:
            entry = self._index.get(name)
            if entry is None:
                logger.warning("specialization allows unknown tool %r — skipping", name)
                continue
            schema, module_path = entry
            desc = ""
            mod = importlib.import_module(module_path)
            for t in getattr(mod, _attr_for(module_path), []) or []:
                if isinstance(t, dict) and t.get("name") == name:
                    desc = t.get("description", "")
                    break
            specs.append(ToolSpec(name=name, description=desc, parameters=schema))
        return specs

    async def execute(self, name: str, arguments: dict[str, Any]) -> str:
        """Run a tool call, returning a string the model can read."""
        entry = self._index.get(name)
        if entry is None:
            return f"error: unknown tool {name!r}"
        _schema, module_path = entry
        executor = self._executor_for(module_path)
        if executor is None:
            return f"error: no executor for tool {name!r}"
        try:
            result = await executor.execute(name, arguments or {})
        except Exception as e:  # noqa: BLE001 — tool failure is reported to the model, not fatal
            logger.warning("tool %s raised: %s", name, e)
            return f"error: tool {name} failed: {e}"
        if isinstance(result, str):
            return result
        return json.dumps(result, default=str)

    def _executor_for(self, module_path: str) -> Any | None:
        if module_path in self._executors:
            return self._executors[module_path]
        mod = importlib.import_module(module_path)
        cls = next(
            (getattr(mod, a) for a in dir(mod) if a.endswith("ToolExecutor") and isinstance(getattr(mod, a), type)),
            None,
        )
        if cls is None:
            self._executors[module_path] = None
            return None
        try:
            inst = cls(self._scm)  # executors that accept an SCM client
        except TypeError:
            inst = cls()  # executors that take no args
        self._executors[module_path] = inst
        return inst


def _attr_for(module_path: str) -> str:
    for _category, mp, attr in _SOURCES:
        if mp == module_path:
            return attr
    return ""


__all__ = ["ToolDispatcher"]
