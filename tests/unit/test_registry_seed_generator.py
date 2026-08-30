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
    assert manifest["metadata"]["tag"] == "1.0.1"
    assert manifest["metadata"]["labels"]["ai.tesserix.dev/runtime"] == "tesserix-adk"
    assert manifest["spec"]["model"] == {"provider": "devai-user-routing", "name": "dynamic"}
    assert manifest["spec"]["a2a"]["url"] == (
        "http://agentgateway-mcp.agentgateway-system.svc.cluster.local:8082/a2a/v1/requirements-analyst"
    )
    assert manifest["spec"]["a2a"]["preferredTransport"] == "JSONRPC"
    assert manifest["spec"]["skills"] == ["requirements-analyst"]
    assert manifest["spec"]["prompts"] == ["requirements-analyst-prompt-v1"]
    assert manifest["spec"]["promptRef"] == "requirements-analyst-prompt-v1"
    assert "runtime" not in manifest["spec"]
    assert "llm" not in manifest["spec"]


def test_default_eval_seeds_are_owned_and_capability_aware() -> None:
    generator = _generator()
    source = yaml.safe_load((REPO_ROOT / "specializations/orchestration/release_manager.yaml").read_text())

    suite = generator._eval_suite_doc(source)
    dataset = generator._dataset_doc(source)

    assert suite["metadata"]["labels"]["devai.io/agent"] == "release-manager-agent"
    assert suite["metadata"]["labels"]["devai.io/generated"] == "true"
    assert suite["spec"]["datasetRef"] == {
        "ref": "release-manager-golden",
        "version": "1",
    }
    assert dataset["metadata"]["labels"]["devai.io/agent"] == "release-manager-agent"
    cases = {case["name"]: case for case in dataset["spec"]["cases"]}
    assert set(cases) == {"happy-path", "prompt-injection", "should-refuse", "tool-failure"}
    assert cases["happy-path"]["expect"]["tools_called"] == ["scm_get_pull_request"]
    assert "scm_merge_pull_request" in cases["should-refuse"]["expect"]["tools_not_called"]
    assert "scm_get_pull_request" not in cases["should-refuse"]["expect"]["tools_not_called"]


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


def test_all_generated_agents_reference_seeded_skill_and_prompt_artifacts() -> None:
    seeds = REPO_ROOT / "architecture" / "registry-seeds"

    for path in sorted((seeds / "agents").glob("*.yaml")):
        agent = yaml.safe_load(path.read_text())
        name = agent["metadata"]["name"].removesuffix("-agent")
        prompt = f"{name}-prompt-v1"

        assert agent["spec"]["skills"] == [name]
        assert agent["spec"]["prompts"] == [prompt]
        assert agent["spec"]["promptRef"] == prompt
        assert (seeds / "skills" / f"{name}.yaml").is_file()
        assert (seeds / "prompts" / f"{prompt}.yaml").is_file()


def test_all_40_generated_agents_have_owned_golden_evaluations() -> None:
    seed_root = REPO_ROOT / "architecture" / "registry-seeds"
    agent_names = {
        yaml.safe_load(path.read_text())["metadata"]["name"] for path in (seed_root / "agents").glob("*.yaml")
    }
    suite_agents = {
        yaml.safe_load(path.read_text())["metadata"]["labels"]["devai.io/agent"]
        for path in (seed_root / "eval-suites").glob("*.yaml")
    }
    dataset_agents = {
        yaml.safe_load(path.read_text())["metadata"]["labels"]["devai.io/agent"]
        for path in (seed_root / "datasets").glob("*.yaml")
    }

    assert len(agent_names) == 40
    assert suite_agents == agent_names
    assert dataset_agents == agent_names
