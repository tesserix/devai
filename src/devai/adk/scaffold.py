"""On-disk scaffolds for new catalog entries.

When you want to add a new skill / agent / prompt / MCP server, the
first step is dropping a YAML in ``architecture/registry-seeds/<kind>/``
so it survives bootstrap re-runs. These helpers write a sensible
starter file you can edit, instead of asking the operator to remember
the schema.

The on-disk format keeps the K8s-CRD envelope DevAI's runtime expects
— that's the source of truth for both the runtime layer (which loads
local YAML as a fallback when aregistry is unreachable) and the
bootstrap Job (which translates the CRD envelope into aregistry's
JSON shape).
"""

from __future__ import annotations

from pathlib import Path

import yaml

_SEEDS_ROOT_DEFAULT = Path("architecture/registry-seeds")


def _atomic_write(path: Path, content: str) -> None:
    """Write a starter file but never overwrite an existing entry.

    Catalog entries are versioned: editing in place loses history.
    """
    if path.exists():
        raise FileExistsError(f"{path} already exists — bump version or pick a new name")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def scaffold_skill(
    name: str,
    *,
    seeds_root: Path | str = _SEEDS_ROOT_DEFAULT,
    description: str = "",
    category: str = "review",
    version: str = "1",
) -> Path:
    """Write ``<seeds_root>/skills/<name>.yaml``."""
    doc = {
        "apiVersion": "registry.solo.io/v1alpha1",
        "kind": "Skill",
        "metadata": {
            "name": name,
            "namespace": "devai",
            "labels": {
                "devai.io/source": "devai",
                "devai.io/category": category,
            },
        },
        "spec": {
            "displayName": name.replace("-", " ").title(),
            "description": description or f"TODO: describe what {name} does",
            "category": category,
            "version": version,
            "tools": [],
            "handoverSchema": {},
            "contextKeys": [],
        },
    }
    return _emit("skills", name, doc, seeds_root)


def scaffold_prompt(
    name: str,
    *,
    seeds_root: Path | str = _SEEDS_ROOT_DEFAULT,
    content: str = "",
    description: str = "",
    version: str = "1",
) -> Path:
    """Write ``<seeds_root>/prompts/<name>.yaml``."""
    doc = {
        "apiVersion": "registry.solo.io/v1alpha1",
        "kind": "Prompt",
        "metadata": {
            "name": name,
            "namespace": "devai",
            "labels": {"devai.io/source": "devai"},
        },
        "spec": {
            "version": version,
            "description": description or f"TODO: describe the prompt {name}",
            "template": content or "TODO: replace with the prompt body.",
        },
    }
    return _emit("prompts", name, doc, seeds_root)


def scaffold_mcp_server(
    name: str,
    *,
    seeds_root: Path | str = _SEEDS_ROOT_DEFAULT,
    description: str = "",
    endpoint: str = "",
    version: str = "1",
) -> Path:
    """Write ``<seeds_root>/mcp-servers/<name>.yaml``."""
    doc = {
        "apiVersion": "registry.solo.io/v1alpha1",
        "kind": "MCPServer",
        "metadata": {
            "name": name,
            "namespace": "devai",
            "labels": {"devai.io/source": "devai"},
        },
        "spec": {
            "displayName": name.replace("-", " ").title(),
            "description": description or f"TODO: describe MCP server {name}",
            "endpoint": endpoint or "http://CHANGE.svc.cluster.local:8080/mcp",
            "version": version,
            "authMode": "jwt",
            "tools": [],
        },
    }
    return _emit("mcp-servers", name, doc, seeds_root)


def scaffold_agent(
    name: str,
    *,
    seeds_root: Path | str = _SEEDS_ROOT_DEFAULT,
    description: str = "",
    skill: str = "",
    prompt: str = "",
    model_provider: str = "anthropic",
    model: str = "claude-sonnet-5",
    version: str = "1",
) -> Path:
    """Write ``<seeds_root>/agents/<name>.yaml``."""
    doc = {
        "apiVersion": "registry.solo.io/v1alpha1",
        "kind": "Agent",
        "metadata": {
            "name": name,
            "namespace": "devai",
            "labels": {
                "devai.io/source": "devai",
                **({"devai.io/skill": skill} if skill else {}),
            },
        },
        "spec": {
            "description": description or f"TODO: describe {name}",
            "version": version,
            "skill": skill,
            "promptRef": prompt,
            "llm": {
                "provider": model_provider,
                "model": model,
                "temperature": None,
                "maxTokens": None,
            },
            "limits": {"maxTurns": 60, "timeoutSeconds": 900},
            "runtime": {
                "kind": "python_class",
                "pythonClass": f"devai.agents.{name.replace('-', '_')}.{_to_class_name(name)}",
            },
            "riskLevel": "low",
            "roleColor": "engineer",
        },
    }
    return _emit("agents", name, doc, seeds_root)


def _emit(subdir: str, name: str, doc: dict, seeds_root: Path | str) -> Path:
    root = Path(seeds_root)
    path = root / subdir / f"{name}.yaml"
    body = yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)
    _atomic_write(path, body)
    return path


def _to_class_name(name: str) -> str:
    """foo-bar-baz → FooBarBaz."""
    return "".join(p.capitalize() for p in name.replace("_", "-").split("-"))
