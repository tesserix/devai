"""Pure mapping from devai's authoring models to agentic-registry envelopes.

The registry stores Kubernetes-style objects (apiVersion/kind/metadata/spec).
These functions translate a devai Specialization / Blueprint / skill-form /
mcp-form into that envelope so authored artifacts land in the shared catalog
with the composition shape the registry's Agent editor + A2A card understand.

No I/O here — kept pure so it's trivially unit-testable.
"""

from __future__ import annotations

import re
from typing import Any

API_VERSION = "registry.agentic.dev/v1alpha1"

# Label marking artifacts that devai authored (so the catalog can filter them).
AUTHORED_LABEL = "devai.tesserix.app/authored"


def _safe_label(value: str) -> str:
    """Coerce an arbitrary string (e.g. an email) into an RFC-1123 label value."""
    v = re.sub(r"[^a-zA-Z0-9._-]", "-", value or "").strip("-._")
    return v[:63] or "operator"


# An artifact name must be a DNS-1123 subdomain-ish token: lowercase
# alphanumerics plus `.`/`-`, no leading/trailing separators. The registry
# enforces this on its side; normalizing here keeps authored names predictable
# and stops path/registry-hostile input (spaces, slashes, ../) at the door.
_NAME_INVALID_RE = re.compile(r"[^a-z0-9._-]+")


def normalize_artifact_name(name: str) -> str:
    """Normalize an authored artifact name to a registry-safe token.

    Lowercases, replaces runs of disallowed characters with a single ``-``,
    and trims leading/trailing separators. Raises ValueError when nothing
    usable remains (an all-invalid name) so the caller surfaces a 422 rather
    than persisting an empty/garbage key.
    """
    raw = (name or "").strip().lower()
    cleaned = _NAME_INVALID_RE.sub("-", raw).strip("-._")
    cleaned = cleaned[:253]
    if not cleaned:
        raise ValueError(f"name {name!r} normalizes to empty (no valid characters)")
    return cleaned


def _base_metadata(name: str, *, tenant: str, created_by: str, version: str = "") -> dict[str, Any]:
    meta: dict[str, Any] = {
        "name": name,
        "namespace": tenant,
        "visibility": "private",
        "labels": {
            AUTHORED_LABEL: "true",
            "devai.tesserix.app/created-by": _safe_label(created_by),
        },
    }
    if version:
        meta["tag"] = version
    return meta


def spec_to_agent_envelope(
    spec: Any,
    *,
    tenant: str = "devai",
    created_by: str = "operator",
    version: str = "",
) -> dict[str, Any]:
    """devai Specialization -> registry Agent envelope.

    Registry-composition references (skills/prompts/mcpServers) are carried on
    the Specialization's ``metadata`` dict (the loader passes ``metadata``
    through untouched), written there by the Create-Agent form.
    """
    md = getattr(spec, "metadata", None) or {}
    provider = getattr(getattr(spec, "llm_provider", None), "value", None) or getattr(spec, "llm_provider", "") or ""

    model: dict[str, Any] = {}
    if provider and provider != "auto":
        model["provider"] = provider
    if getattr(spec, "llm_model", ""):
        model["name"] = spec.llm_model
    if getattr(spec, "temperature", None) is not None:
        model["temperature"] = spec.temperature

    agent_spec: dict[str, Any] = {
        "title": getattr(spec, "display_name", "") or getattr(spec, "name", ""),
        "description": getattr(spec, "description", "") or "",
        "systemPrompt": getattr(spec, "system_prompt", "") or "",
        # Registry composition references (names in the catalog).
        "skills": list(md.get("registry_skills") or []),
        "tools": list(md.get("registry_tools") or []),
        "mcpServers": list(md.get("registry_mcp_servers") or []),
        "prompts": list(md.get("registry_prompts") or []),
        # Built-in tool names recorded for provenance (the runner binds them locally).
        "builtinTools": list(getattr(spec, "allowed_tools", None) or []),
    }
    if model:
        agent_spec["model"] = model
    a2a = md.get("a2a")
    if isinstance(a2a, dict) and a2a:
        agent_spec["a2a"] = a2a
    # devai-specific extensions pass through the free-form spec untouched.
    for k_src, k_dst in (
        ("category", "category"),
        ("risk_level", "riskLevel"),
        ("output_key", "outputKey"),
        ("max_turns", "maxTurns"),
        ("timeout", "timeout"),
    ):
        v = getattr(spec, k_src, None)
        if v not in (None, ""):
            agent_spec[k_dst] = v
    handover = getattr(spec, "handover_schema", None)
    if isinstance(handover, dict) and handover:
        agent_spec["handoverSchema"] = handover

    return {
        "apiVersion": API_VERSION,
        "kind": "Agent",
        "metadata": _base_metadata(spec.name, tenant=tenant, created_by=created_by, version=version),
        "spec": agent_spec,
    }


def blueprint_to_blueprint_envelope(
    bp: Any,
    yaml_text: str,
    *,
    tenant: str = "devai",
    created_by: str = "operator",
    version: str = "",
) -> dict[str, Any]:
    """devai Blueprint -> registry Blueprint envelope.

    Stages become graph nodes; ``depends_on`` becomes edges (so the registry's
    FlowCanvas renders it). The canonical devai YAML is stashed under
    ``spec.devaiBlueprintYaml`` so a round-trip back to the executor is lossless.
    """
    stages = list(getattr(bp, "stages", None) or [])
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    for st in stages:
        sid = getattr(st, "name", "") or ""
        nodes.append(
            {
                "id": sid,
                "label": getattr(st, "display_title", None) and st.display_title() or sid,
                "type": getattr(st, "type", "") or "task",
            }
        )
        for dep in getattr(st, "depends_on", None) or []:
            edges.append({"from": dep, "to": sid})

    return {
        "apiVersion": API_VERSION,
        "kind": "Blueprint",
        "metadata": _base_metadata(bp.name, tenant=tenant, created_by=created_by, version=version),
        "spec": {
            "title": getattr(bp, "display_name", "") or getattr(bp, "name", ""),
            "description": getattr(bp, "description", "") or "",
            "nodes": nodes,
            "edges": edges,
            "devaiBlueprintYaml": yaml_text,
        },
    }


def skill_fields_to_skill_envelope(
    fields: dict[str, Any], *, tenant: str = "devai", created_by: str = "operator"
) -> dict[str, Any]:
    """Create-Skill form -> registry Skill envelope."""
    spec: dict[str, Any] = {
        "title": fields.get("title") or fields.get("display_name") or fields["name"],
        "description": fields.get("description", "") or "",
    }
    if fields.get("category"):
        spec["category"] = fields["category"]
    if fields.get("tags"):
        spec["tags"] = list(fields["tags"])
    if fields.get("repository"):
        spec["source"] = {"repository": fields["repository"]}
    return {
        "apiVersion": API_VERSION,
        "kind": "Skill",
        "metadata": _base_metadata(fields["name"], tenant=tenant, created_by=created_by),
        "spec": spec,
    }


def mcp_fields_to_mcpserver_envelope(
    fields: dict[str, Any], *, tenant: str = "devai", created_by: str = "operator"
) -> dict[str, Any]:
    """Create-Tool (MCP server) form -> registry MCPServer envelope.

    Two modes: a remote endpoint (URL) or a container image the platform runs.
    """
    spec: dict[str, Any] = {
        "name": fields["name"],
        "version": str(fields.get("version") or "1.0.0"),
        "description": fields.get("description", "") or "",
    }
    if fields.get("url"):
        spec["remotes"] = [
            {
                "type": fields.get("transport", "streamableHttp"),
                "url": fields["url"],
                **({"headers": fields["headers"]} if fields.get("headers") else {}),
            }
        ]
        spec["type"] = "remote"
    if fields.get("image"):
        spec["image"] = fields["image"]
        spec["type"] = spec.get("type", "image")
        if fields.get("command"):
            spec["command"] = fields["command"]
        if fields.get("args"):
            spec["args"] = list(fields["args"])
        if fields.get("env"):
            spec["env"] = dict(fields["env"])
    return {
        "apiVersion": API_VERSION,
        "kind": "MCPServer",
        "metadata": _base_metadata(fields["name"], tenant=tenant, created_by=created_by),
        "spec": spec,
    }


__all__ = [
    "spec_to_agent_envelope",
    "blueprint_to_blueprint_envelope",
    "skill_fields_to_skill_envelope",
    "mcp_fields_to_mcpserver_envelope",
    "normalize_artifact_name",
    "AUTHORED_LABEL",
]
