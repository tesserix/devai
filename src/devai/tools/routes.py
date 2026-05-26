"""Catalog endpoint for built-in DevAI tools.

The aregistry catalogs *external* assets (skills, prompts, mcp servers,
agents). Tools are different — they're declared in-process inside the
DevAI service (``src/devai/tools/*.py``) because they're tightly bound
to the Python handlers that execute them. This route exposes them under
the same ``/api/catalog/*`` family so the dashboard can render a unified
"what's available" view.

Mounted at ``/api/catalog/tools``. One endpoint, two views:

  GET /api/catalog/tools           Flat list with a ``category`` field.
  GET /api/catalog/tools/by-category  ``{category: [tools]}``.

Tools are grouped by source file:

  scm           Issues/PRs/files (scm_tools.py)
  validation    Input/output guardrails (validation_tools.py)
  document      PDF/spec parsing (document_tools.py)
  security      SAST/SCA/secret scans (security_tools.py)
  test          Playwright/unit harness (test_tools.py)
  file          Local filesystem (file_tools.py)
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/catalog", tags=["catalog"])


# Lazy-imported in _all_tools() so we don't pay the import cost (and the
# transitive httpx / pydantic etc.) just because the FastAPI app loaded.
_TOOL_SOURCES = (
    ("scm", "devai.tools.scm_tools", "SCM_TOOLS"),
    ("validation", "devai.tools.validation_tools", "VALIDATION_TOOLS"),
    ("document", "devai.tools.document_tools", "DOCUMENT_TOOLS"),
    ("security", "devai.tools.security_tools", "SECURITY_TOOLS"),
    ("test", "devai.tools.test_tools", "TEST_TOOLS"),
    ("file", "devai.tools.file_tools", "FILE_TOOLS"),
)


def _all_tools() -> list[dict[str, Any]]:
    import importlib

    out: list[dict[str, Any]] = []
    for category, module_path, attr in _TOOL_SOURCES:
        try:
            mod = importlib.import_module(module_path)
        except Exception:
            logger.warning("catalog: tool module %s not importable — skipped", module_path, exc_info=True)
            continue
        tools = getattr(mod, attr, None)
        if not isinstance(tools, list):
            continue
        for t in tools:
            if not isinstance(t, dict):
                continue
            schema = t.get("input_schema") or {}
            required = schema.get("required") or [] if isinstance(schema, dict) else []
            properties = schema.get("properties") or {} if isinstance(schema, dict) else {}
            out.append(
                {
                    "name": t.get("name", ""),
                    "description": t.get("description", ""),
                    "category": category,
                    "required": list(required),
                    "parameters": list(properties.keys()),
                    "schema": schema,
                }
            )
    out.sort(key=lambda r: (r["category"], r["name"]))
    return out


@router.get("/tools")
async def list_tools(category: str = "") -> list[dict[str, Any]]:
    """List every built-in DevAI tool.

    Optional ``category`` filter narrows to one group (``scm``,
    ``validation``, ``document``, ``security``, ``test``, ``file``).
    """
    tools = _all_tools()
    if category:
        tools = [t for t in tools if t["category"] == category]
    return tools


@router.get("/tools/by-category")
async def list_tools_by_category() -> dict[str, list[dict[str, Any]]]:
    """Same data as ``/tools``, grouped under ``{category: [...]}``."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for t in _all_tools():
        grouped.setdefault(t["category"], []).append(t)
    return grouped


@router.get("/counts")
async def catalog_counts() -> dict[str, int]:
    """Single-call counts for the dashboard tiles.

    Covers only the categories DevAI owns end-to-end: tools, blueprints,
    specializations, and registered stages. aregistry-backed catalogs
    (skills/prompts/mcp-servers/agents) keep their existing /api/registry/counts.
    """
    counts: dict[str, int] = {"tools": len(_all_tools())}

    # Blueprints — files on disk; cheaper than asking PipelineService
    # in case it's disabled.
    try:
        from pathlib import Path

        # The blueprint dir setting is exposed on Settings; fall back to
        # the repo-relative "blueprints" so non-prod configs work too.
        try:
            from devai.config import get_settings

            settings = get_settings()
            bp_dir = Path(getattr(settings, "pipeline_blueprint_dir", "blueprints"))
        except Exception:
            bp_dir = Path("blueprints")
        if bp_dir.exists():
            counts["blueprints"] = sum(1 for p in bp_dir.glob("*.yaml"))
        else:
            counts["blueprints"] = 0
    except Exception:
        counts["blueprints"] = 0

    # Specializations — also files on disk.
    try:
        from pathlib import Path

        spec_dir = Path("specializations")
        if spec_dir.exists():
            counts["specializations"] = sum(1 for p in spec_dir.glob("*.yaml"))
        else:
            counts["specializations"] = 0
    except Exception:
        counts["specializations"] = 0

    # Registered stages — count from the default registry. Build a fresh
    # registry so this works even when PipelineService is disabled.
    try:
        from devai.blueprint.registry import StageRegistry, register_defaults

        reg = StageRegistry()
        register_defaults(reg)
        counts["stages"] = len(reg.known_stages())
    except Exception:
        counts["stages"] = 0

    return counts


@router.get("/stages")
async def list_stages() -> list[dict[str, str]]:
    """List every registered stage key, sorted alphabetically."""
    try:
        from devai.blueprint.registry import StageRegistry, register_defaults

        reg = StageRegistry()
        register_defaults(reg)
        return [{"name": k} for k in reg.known_stages()]
    except Exception as e:
        logger.warning("catalog: stage list failed: %s", e)
        return []


__all__ = ["router"]
