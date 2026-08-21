from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]


def _generator() -> ModuleType:
    path = REPO_ROOT / "scripts" / "generate_registry_seeds.py"
    spec = importlib.util.spec_from_file_location("generate_registry_seeds", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_agent_seed_uses_current_a2a_schema_and_dynamic_user_routing() -> None:
    generator = _generator()
    source = yaml.safe_load((REPO_ROOT / "specializations/planning/requirements_analyst.yaml").read_text())

    manifest = generator._agent_doc(source)

    assert manifest["apiVersion"] == "registry.agentic.dev/v1alpha1"
    assert manifest["metadata"]["tenantId"] == "devai"
    assert manifest["metadata"]["tag"] == "1.0.0"
    assert manifest["metadata"]["labels"]["ai.tesserix.dev/runtime"] == "tesserix-adk"
    assert manifest["spec"]["model"] == {"provider": "devai-user-routing", "name": "dynamic"}
    assert manifest["spec"]["a2a"]["url"].endswith("/a2a/v1/requirements-analyst")
    assert manifest["spec"]["a2a"]["preferredTransport"] == "JSONRPC"
    assert manifest["spec"]["skills"][0]["id"] == "requirements-analyst"
    assert "runtime" not in manifest["spec"]
    assert "llm" not in manifest["spec"]


def test_all_40_generated_agent_seeds_are_tesserix_adk_a2a_agents() -> None:
    generator = _generator()
    manifests = [
        generator._agent_doc(yaml.safe_load(path.read_text()))
        for path in sorted((REPO_ROOT / "specializations").rglob("*.yaml"))
        if not path.name.startswith("_")
    ]

    assert len(manifests) == 40
    assert {manifest["metadata"]["labels"]["ai.tesserix.dev/runtime"] for manifest in manifests} == {"tesserix-adk"}
    assert len({manifest["spec"]["a2a"]["url"] for manifest in manifests}) == 40
