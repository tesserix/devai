#!/usr/bin/env python3
"""Generate first-class ``kind: Tool`` registry seeds from the MCP servers.

Phase 1 of the DevAI MCP Hub design (docs/agentic/MCP-HUB.md): every tool a
DevAI MCP server exposes becomes a standalone Tool artifact, labelled to its
server (``mcp.devai.io/server``) so an MCPServer can select its tools by label
instead of a hardcoded string list. Input schemas come from devai's real tool
registry where available, derived otherwise.

Run:  PYTHONPATH=src python architecture/registry-seeds/_import/generate_tools.py
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
SEEDS = HERE.parents[0]
MCP_DIR = SEEDS / "mcp-servers"
OUT_DIR = SEEDS / "tools"


def _slug(v: str) -> str:
    return re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9]+", "-", v.lower()).strip("-"))[:63].strip("-")


def _title(name: str) -> str:
    return name.replace("_", " ").replace("-", " ").strip().title()


def _load_tool_schemas() -> dict[str, dict]:
    """Import devai's tool modules and dump {name: {description, parameters}}."""
    from devai.tools import registry as R
    for m in ("scm_tools", "github_tools", "file_tools", "document_tools",
              "checkpoint_tools", "shell_tools", "web_tools", "security_tools",
              "validation_tools", "gitops_tools", "dispatch"):
        try:
            importlib.import_module(f"devai.tools.{m}")
        except Exception:  # noqa: BLE001 — best-effort; missing dep just means fewer schemas
            pass
    out: dict[str, dict] = {}
    for name, rt in getattr(R, "_REGISTRY", {}).items():
        spec = getattr(rt, "spec", rt)
        out[name] = {
            "description": getattr(rt, "description", "") or getattr(spec, "description", ""),
            "parameters": getattr(rt, "parameters", None) or getattr(spec, "parameters", None) or {},
        }
    return out


def _domain(tool: str, server_short: str) -> str:
    if tool.startswith("security_"):
        return "security"
    if tool.startswith("validate_"):
        return "quality"
    if tool.startswith("scm_"):
        return "scm"
    if tool.startswith(("argocd_", "kargo_", "flux_")):
        return "gitops"
    if tool.startswith("sre_") or server_short == "sre":
        return "sre"
    if tool.startswith("devai_pipeline_"):
        return "pipeline"
    if tool.startswith("devai_specializations_"):
        return "catalog"
    if tool.startswith("devai_"):
        return "platform"
    if tool.startswith("sample_"):
        return "sample"
    return "general"


def _risk(tool: str) -> str:
    hot = ("commit", "merge", "create_pr", "create_pull", "create_branch", "close_issue",
           "dispatch", "scan", "sbom", "delete", "rollback", "promote", "sync",
           "reconcile", "suspend")
    return "medium" if any(h in tool for h in hot) else "low"


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for old in OUT_DIR.glob("*.yaml"):
        old.unlink()

    schemas = _load_tool_schemas()
    print(f"loaded {len(schemas)} real tool schemas from devai.tools")

    written, with_schema = 0, 0
    seen: set[str] = set()
    for mcp_file in sorted(MCP_DIR.glob("*.yaml")):
        doc = yaml.safe_load(mcp_file.read_text())
        if doc.get("kind") != "MCPServer":
            continue
        server = doc["metadata"]["name"]
        server_short = re.sub(r"-mcp$", "", server)
        spec = doc.get("spec", {})
        for tool in spec.get("tools", []):
            name = _slug(f"{server_short}-{tool}")
            if name in seen:
                continue
            seen.add(name)
            dom = _domain(tool, server_short)
            sch = schemas.get(tool)
            if sch:
                with_schema += 1
                desc = sch["description"] or f"{_title(tool)} — tool served by {server}."
                input_schema = sch["parameters"] or {"type": "object", "properties": {}}
            else:
                desc = f"{_title(tool)} — tool served by the {server_short} MCP server."
                input_schema = {"type": "object", "properties": {}}
            env = {
                "apiVersion": "registry.solo.io/v1alpha1",
                "kind": "Tool",
                "metadata": {
                    "name": name,
                    "namespace": "devai",
                    "visibility": "public",  # catalog metadata; browsable in the UI
                    "labels": {
                        "devai.io/source": "devai",
                        "mcp.devai.io/server": server,
                        "devai.io/domain": dom,
                        "devai.io/tier": "core",
                        "devai.io/risk-level": _risk(tool),
                    },
                    "annotations": {"mcp.devai.io/wire-name": tool},
                },
                "spec": {
                    "displayName": _title(tool),
                    "description": desc[:500],
                    "version": "1",
                    "server": server,
                    "inputSchema": input_schema,
                    "tags": sorted({server_short, dom, "mcp-tool"}),
                },
            }
            (OUT_DIR / f"{name}.yaml").write_text(
                yaml.safe_dump(env, sort_keys=False, default_flow_style=False, width=100),
                encoding="utf-8",
            )
            written += 1
    print(f"wrote {written} Tool seeds ({with_schema} with real schemas) -> {OUT_DIR}")


if __name__ == "__main__":
    main()
