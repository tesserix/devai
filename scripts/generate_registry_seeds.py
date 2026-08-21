#!/usr/bin/env python3
"""Generate registry-seeds/ CRs from specializations/*.yaml.

Reads every specialization in the catalog and emits three corresponding
manifests under `architecture/registry-seeds/`:

    skills/<name>.yaml        — Skill CR (the capability)
    agents/<name>-agent.yaml  — Agent CR (the runtime that delivers it)
    prompts/<name>-prompt-v1.yaml — Prompt CR (extracted system prompt)

Run via `make registry-seeds`. Idempotent — re-runs overwrite existing
files. The CI guard `make registry-seeds-check` calls this in --check
mode and fails the build on drift.

The generator is intentionally a single file, no external deps beyond
PyYAML, so the CI image can run it without installing the full DevAI
dependency tree.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SPECS_DIR = REPO_ROOT / "specializations"
SEEDS_DIR = REPO_ROOT / "architecture" / "registry-seeds"

CATALOG_API_VERSION = "registry.solo.io/v1alpha1"
AGENT_API_VERSION = "registry.agentic.dev/v1alpha1"
NAMESPACE = "devai"
SOURCE_LABEL = "devai"
A2A_BASE_URL = "http://devai-api.devai.svc.cluster.local:8080/a2a/v1"
# Seeds are public so they show in the (logged-in) aregistry marketplace, not
# just DevAI's own authenticated reads. aregistry defaults an unset visibility
# to "private", which CanRead hides from anyone outside the owning tenant.
VISIBILITY = "public"


def _kebab(name: str) -> str:
    return name.replace("_", "-")


def _to_yaml(doc: dict[str, Any]) -> str:
    return yaml.safe_dump(doc, sort_keys=False, default_flow_style=False, width=100)


def _prompt_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12]


def _skill_doc(spec: dict[str, Any]) -> dict[str, Any]:
    name = _kebab(spec["name"])
    category = spec.get("category", "specialist")
    return {
        "apiVersion": CATALOG_API_VERSION,
        "kind": "Skill",
        "metadata": {
            "name": name,
            "namespace": NAMESPACE,
            "visibility": VISIBILITY,
            "labels": {
                "devai.io/source": SOURCE_LABEL,
                "devai.io/category": category,
                "devai.io/risk-level": spec.get("risk_level", "medium"),
            },
        },
        "spec": {
            "displayName": spec.get("display_name", name),
            "description": (spec.get("description") or "").strip(),
            "category": category,
            "tools": list(spec.get("allowed_tools") or []),
            "handoverSchema": spec.get("handover_schema") or {},
            "contextKeys": list(spec.get("context_keys") or []),
            "outputKey": spec.get("output_key") or f"{spec['name']}_output",
            "metadata": dict(spec.get("metadata") or {}),
        },
    }


def _agent_doc(spec: dict[str, Any]) -> dict[str, Any]:
    name = _kebab(spec["name"])
    category = str(spec.get("category") or "specialist")
    risk = str(spec.get("risk_level") or "medium")
    title = str(spec.get("display_name") or name.replace("-", " ").title())
    description = str(spec.get("description") or "").strip()
    version = str((spec.get("metadata") or {}).get("version") or "1.0.0")
    return {
        "apiVersion": AGENT_API_VERSION,
        "kind": "Agent",
        "metadata": {
            "name": f"{name}-agent",
            "namespace": NAMESPACE,
            "tenantId": NAMESPACE,
            "tag": version,
            "visibility": VISIBILITY,
            "labels": {
                "devai.io/source": SOURCE_LABEL,
                "devai.io/category": category,
                "devai.io/skill": name,
                "devai.io/risk-level": risk,
                "ai.tesserix.dev/runtime": "tesserix-adk",
                "ai.tesserix.dev/provider-policy": "user-connectors",
            },
        },
        "spec": {
            "title": title,
            "description": description,
            "model": {
                "provider": "devai-user-routing",
                "name": "dynamic",
            },
            "a2a": {
                "url": f"{A2A_BASE_URL}/{name}",
                "preferredTransport": "JSONRPC",
                "provider": {
                    "organization": "Tesserix",
                    "url": "https://tesserix.app",
                },
                "capabilities": {
                    "streaming": False,
                    "pushNotifications": False,
                },
                "defaultInputModes": ["application/json", "text/plain"],
                "defaultOutputModes": ["application/json", "text/plain"],
            },
            "skills": [
                {
                    "id": name,
                    "name": title,
                    "description": description,
                    "tags": [category, risk, "devai"],
                }
            ],
        },
    }


def _prompt_doc(spec: dict[str, Any]) -> dict[str, Any]:
    name = _kebab(spec["name"])
    prompt = spec.get("system_prompt") or ""
    return {
        "apiVersion": CATALOG_API_VERSION,
        "kind": "Prompt",
        "metadata": {
            "name": f"{name}-prompt-v1",
            "namespace": NAMESPACE,
            "visibility": VISIBILITY,
            "labels": {
                "devai.io/source": SOURCE_LABEL,
                "devai.io/skill": name,
                "devai.io/prompt-hash": _prompt_hash(prompt),
            },
        },
        "spec": {
            "version": 1,
            "skill": name,
            "systemPrompt": prompt,
            "userPromptTemplate": spec.get("user_prompt_template") or "",
        },
    }


def _load_spec(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _write_doc(path: Path, doc: dict[str, Any]) -> bool:
    """Write doc to path. Returns True if file changed."""
    new = _to_yaml(doc)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if new == existing:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new, encoding="utf-8")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Don't write — exit 1 if any file would change. For CI.",
    )
    args = parser.parse_args()

    if not SPECS_DIR.exists():
        print(f"ERROR: {SPECS_DIR} not found", file=sys.stderr)
        return 1

    drift: list[str] = []
    written: list[str] = []
    for spec_path in sorted(SPECS_DIR.rglob("*.yaml")):
        if spec_path.name.startswith("_"):
            continue
        spec = _load_spec(spec_path)
        if not isinstance(spec, dict) or "name" not in spec:
            continue
        name = _kebab(spec["name"])

        targets = [
            (SEEDS_DIR / "skills" / f"{name}.yaml", _skill_doc(spec)),
            (SEEDS_DIR / "agents" / f"{name}-agent.yaml", _agent_doc(spec)),
            (SEEDS_DIR / "prompts" / f"{name}-prompt-v1.yaml", _prompt_doc(spec)),
        ]
        for target, doc in targets:
            if args.check:
                new_yaml = _to_yaml(doc)
                existing = target.read_text(encoding="utf-8") if target.exists() else ""
                if new_yaml != existing:
                    drift.append(str(target.relative_to(REPO_ROOT)))
            else:
                if _write_doc(target, doc):
                    written.append(str(target.relative_to(REPO_ROOT)))

    if args.check:
        if drift:
            print(
                f"registry-seeds drift detected ({len(drift)} files would change):",
                file=sys.stderr,
            )
            for f in drift:
                print(f"  {f}", file=sys.stderr)
            print(
                "Run `make registry-seeds` to regenerate, then commit.",
                file=sys.stderr,
            )
            return 1
        print(f"registry-seeds in sync ({len(list(SEEDS_DIR.rglob('*.yaml')))} files)")
        return 0

    print(f"Wrote {len(written)} new/changed seed manifests")
    for f in written:
        print(f"  {f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
