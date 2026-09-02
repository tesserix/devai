"""Resolve the smallest safe Agent evaluation matrix for a repository diff."""

from __future__ import annotations

import argparse
import json
import subprocess
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

from devai.services.redact import scrub, scrub_structure


def _document(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return value if isinstance(value, dict) else {}


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _reference_names(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value} if value else set()
    if isinstance(value, dict):
        reference = value.get("ref") or value.get("name")
        return {str(reference)} if reference else set()
    if isinstance(value, list):
        names: set[str] = set()
        for item in value:
            names.update(_reference_names(item))
        return names
    return set()


def _blueprint_agent_references(document: dict[str, Any]) -> set[str]:
    spec = _mapping(document.get("spec")) if document.get("kind") in {"Blueprint", "Workflow"} else document
    references = _reference_names(spec.get("agents")) | _reference_names(spec.get("agentRefs"))
    stages = spec.get("stages") or []
    if not isinstance(stages, list):
        return references
    for value in stages:
        stage = _mapping(value)
        references.update(_reference_names(stage.get("agent")))
        references.update(_reference_names(_mapping(stage.get("config")).get("agent")))
    return references


def _artifact_index(root: Path, repo_root: Path) -> dict[str, tuple[str, dict[str, Any]]]:
    indexed: dict[str, tuple[str, dict[str, Any]]] = {}
    for path in sorted((*root.rglob("*.yaml"), *root.rglob("*.yml"))):
        document = _document(path)
        metadata = _mapping(document.get("metadata"))
        spec = _mapping(document.get("spec"))
        name = str(metadata.get("name") or spec.get("name") or path.stem)
        indexed[name] = (path.relative_to(repo_root).as_posix(), document)
    return indexed


def resolve_impact(repo_root: Path, changed_paths: Iterable[str]) -> dict[str, list[Any]]:
    changed = [Path(path).as_posix() for path in changed_paths]
    changed_set = set(changed)
    agents_root = repo_root / "architecture/registry-seeds/agents"
    skills_root = repo_root / "architecture/registry-seeds/skills"
    prompts_root = repo_root / "architecture/registry-seeds/prompts"
    tools_root = repo_root / "architecture/registry-seeds/tools"
    mcp_root = repo_root / "architecture/registry-seeds/mcp-servers"
    suites_root = repo_root / "architecture/registry-seeds/eval-suites"
    datasets_root = repo_root / "architecture/registry-seeds/datasets"
    skills_index = _artifact_index(skills_root, repo_root)
    prompts_index = _artifact_index(prompts_root, repo_root)
    tools_index = _artifact_index(tools_root, repo_root)
    mcp_index = _artifact_index(mcp_root, repo_root)
    datasets_index = _artifact_index(datasets_root, repo_root)
    suites_index = _artifact_index(suites_root, repo_root)
    agents: dict[str, tuple[str, dict[str, Any]]] = {}
    prompt_agents: dict[str, set[str]] = {}
    skill_agents: dict[str, set[str]] = {}
    tool_agents: dict[str, set[str]] = {}
    mcp_agents: dict[str, set[str]] = {}
    dataset_agents: dict[str, set[str]] = {}
    suite_agents: dict[str, set[str]] = {}
    python_agents: dict[str, set[str]] = {}
    skill_tools: dict[str, set[str]] = {}
    skill_mcp_servers: dict[str, set[str]] = {}
    mcp_tools: dict[str, set[str]] = {}

    for name, (_, document) in skills_index.items():
        metadata = _mapping(document.get("metadata"))
        spec = _mapping(document.get("spec"))
        skill_tools[name] = _reference_names(spec.get("tools"))
        skill_mcp_servers[name] = _reference_names(spec.get("mcpServers"))

    for name, (_, document) in mcp_index.items():
        mcp_tools[name] = _reference_names(_mapping(document.get("spec")).get("tools"))

    for path in sorted((*agents_root.glob("*.yaml"), *agents_root.glob("*.yml"))):
        document = _document(path)
        metadata = _mapping(document.get("metadata"))
        spec = _mapping(document.get("spec"))
        name = str(metadata.get("name") or spec.get("name") or path.stem)
        relative = path.relative_to(repo_root).as_posix()
        agents[name] = (relative, document)
        labels = _mapping(metadata.get("labels"))
        prompt_references = _reference_names(spec.get("prompts")) | _reference_names(spec.get("promptRef"))
        skill_references = _reference_names(spec.get("skills")) | _reference_names(spec.get("skill"))
        labelled_skill = str(labels.get("devai.io/skill") or "")
        if labelled_skill:
            skill_references.add(labelled_skill)
        for prompt_ref in prompt_references:
            prompt_agents.setdefault(prompt_ref, set()).add(name)
        for skill_ref in skill_references:
            skill_agents.setdefault(skill_ref, set()).add(name)
        for tool_ref in _reference_names(spec.get("tools")):
            tool_agents.setdefault(tool_ref, set()).add(name)
        for mcp_ref in _reference_names(spec.get("mcpServers")):
            mcp_agents.setdefault(mcp_ref, set()).add(name)
        python_class = str(_mapping(spec.get("runtime")).get("pythonClass") or "")
        if python_class:
            python_module = python_class.rsplit(".", maxsplit=1)[0]
            python_agents.setdefault(python_module, set()).add(name)

    for skill_name, consuming_agents in skill_agents.items():
        for tool_ref in skill_tools.get(skill_name, ()):
            tool_agents.setdefault(tool_ref, set()).update(consuming_agents)
        for mcp_ref in skill_mcp_servers.get(skill_name, ()):
            mcp_agents.setdefault(mcp_ref, set()).update(consuming_agents)

    for name, (_, document) in datasets_index.items():
        metadata = _mapping(document.get("metadata"))
        agent_name = str(_mapping(metadata.get("labels")).get("devai.io/agent") or "")
        if agent_name:
            dataset_agents.setdefault(name, set()).add(agent_name)

    suites: dict[str, tuple[str, str, str, bool]] = {}
    for suite_name, (suite_path, document) in suites_index.items():
        metadata = _mapping(document.get("metadata"))
        labels = _mapping(metadata.get("labels"))
        spec = _mapping(document.get("spec"))
        agent_name = str(labels.get("devai.io/agent") or "")
        if agent_name:
            dataset_name = str(_mapping(spec.get("datasetRef")).get("ref") or "")
            dataset_path = datasets_index.get(
                dataset_name,
                (f"architecture/registry-seeds/datasets/{dataset_name}.yaml", {}),
            )[0]
            scorers = spec.get("scorers") or []
            suite_agents.setdefault(suite_name, set()).add(agent_name)
            if dataset_name:
                dataset_agents.setdefault(dataset_name, set()).add(agent_name)
            suites[agent_name] = (
                suite_name,
                suite_path,
                dataset_path,
                isinstance(scorers, list) and "llm_judge" in scorers,
            )

    dependency_paths: dict[str, list[str]] = {}
    for agent_name, (_, document) in agents.items():
        metadata = _mapping(document.get("metadata"))
        labels = _mapping(metadata.get("labels"))
        spec = _mapping(document.get("spec"))
        skills = _reference_names(spec.get("skills")) | _reference_names(spec.get("skill"))
        labelled_skill = str(labels.get("devai.io/skill") or "")
        if labelled_skill:
            skills.add(labelled_skill)
        prompts = _reference_names(spec.get("prompts")) | _reference_names(spec.get("promptRef"))
        tools = _reference_names(spec.get("tools"))
        mcp_servers = _reference_names(spec.get("mcpServers"))
        for skill in skills:
            tools.update(skill_tools.get(skill, ()))
            mcp_servers.update(skill_mcp_servers.get(skill, ()))
        for server in mcp_servers:
            tools.update(mcp_tools.get(server, ()))

        ordered: list[str] = []
        for names, index, directory in (
            (tools, tools_index, "tools"),
            (mcp_servers, mcp_index, "mcp-servers"),
            (skills, skills_index, "skills"),
            (prompts, prompts_index, "prompts"),
        ):
            for reference in sorted(names):
                ordered.append(
                    index.get(
                        reference,
                        (f"architecture/registry-seeds/{directory}/{reference}.yaml", {}),
                    )[0]
                )
        if agent_name in suites:
            ordered.extend((suites[agent_name][2], suites[agent_name][1]))
        dependency_paths[agent_name] = list(dict.fromkeys(path for path in ordered if path))

    affected: set[str] = set()
    for changed_path in changed:
        if changed_path.startswith("architecture/registry-seeds/agents/"):
            document = _document(repo_root / changed_path)
            metadata = _mapping(document.get("metadata"))
            spec = _mapping(document.get("spec"))
            name = str(metadata.get("name") or spec.get("name") or Path(changed_path).stem)
            if name in agents:
                affected.add(name)
        elif changed_path.startswith("architecture/registry-seeds/prompts/"):
            document = _document(repo_root / changed_path)
            metadata = _mapping(document.get("metadata"))
            spec = _mapping(document.get("spec"))
            prompt_name = str(metadata.get("name") or spec.get("name") or Path(changed_path).stem)
            affected.update(prompt_agents.get(prompt_name, ()))
        elif changed_path.startswith("architecture/registry-seeds/skills/"):
            document = _document(repo_root / changed_path)
            metadata = _mapping(document.get("metadata"))
            spec = _mapping(document.get("spec"))
            skill_name = str(metadata.get("name") or spec.get("name") or Path(changed_path).stem)
            affected.update(skill_agents.get(skill_name, ()))
        elif changed_path.startswith("architecture/registry-seeds/tools/"):
            document = _document(repo_root / changed_path)
            metadata = _mapping(document.get("metadata"))
            spec = _mapping(document.get("spec"))
            tool_name = str(metadata.get("name") or spec.get("name") or Path(changed_path).stem)
            affected.update(tool_agents.get(tool_name, ()))
        elif changed_path.startswith("architecture/registry-seeds/mcp-servers/"):
            document = _document(repo_root / changed_path)
            metadata = _mapping(document.get("metadata"))
            spec = _mapping(document.get("spec"))
            mcp_name = str(metadata.get("name") or spec.get("name") or Path(changed_path).stem)
            affected.update(mcp_agents.get(mcp_name, ()))
        elif changed_path.startswith("architecture/registry-seeds/datasets/"):
            document = _document(repo_root / changed_path)
            metadata = _mapping(document.get("metadata"))
            spec = _mapping(document.get("spec"))
            dataset_name = str(metadata.get("name") or spec.get("name") or Path(changed_path).stem)
            affected.update(dataset_agents.get(dataset_name, ()))
        elif changed_path.startswith("architecture/registry-seeds/eval-suites/"):
            document = _document(repo_root / changed_path)
            metadata = _mapping(document.get("metadata"))
            spec = _mapping(document.get("spec"))
            suite_name = str(metadata.get("name") or spec.get("name") or Path(changed_path).stem)
            affected.update(suite_agents.get(suite_name, ()))
        elif changed_path.startswith(
            (
                "architecture/registry-seeds/blueprints/",
                "architecture/registry-seeds/workflows/",
                "blueprints/",
            )
        ):
            document = _document(repo_root / changed_path)
            for reference in _blueprint_agent_references(document):
                if reference in agents:
                    affected.add(reference)
                affected.update(skill_agents.get(reference, ()))
        elif changed_path.startswith("specializations/"):
            if changed_path.startswith("specializations/_common/"):
                affected.update(agents)
            else:
                specialization = _document(repo_root / changed_path)
                skill = str(specialization.get("name") or Path(changed_path).stem).replace("_", "-")
                affected.update(skill_agents.get(skill, ()))
        elif changed_path.startswith("src/devai/agents/"):
            module_path = Path(changed_path).relative_to("src").with_suffix("")
            module = ".".join(module_path.parts)
            if module in {"devai.agents.__init__"} or module.startswith("devai.agents.skills."):
                for names in python_agents.values():
                    affected.update(names)
            else:
                affected.update(python_agents.get(module, ()))

    release_paths: dict[str, list[str]] = {name: [agents[name][0]] for name in agents}
    for changed_path in changed:
        if not changed_path.startswith(
            (
                "architecture/registry-seeds/blueprints/",
                "architecture/registry-seeds/workflows/",
            )
        ):
            continue
        for reference in _blueprint_agent_references(_document(repo_root / changed_path)):
            targets = {reference} if reference in agents else set()
            targets.update(skill_agents.get(reference, ()))
            for target in targets:
                release_paths.setdefault(target, [agents[target][0]]).append(changed_path)

    evaluations = [
        {
            "agent": name,
            "agent_path": agents[name][0],
            "dataset_path": suites[name][2],
            "dependency_paths": dependency_paths[name],
            "changed_dependency_paths": [path for path in dependency_paths[name] if path in changed_set],
            "judge": suites[name][3],
            "release_paths": list(dict.fromkeys(release_paths[name])),
            "suite": suites[name][0],
            "suite_path": suites[name][1],
        }
        for name in sorted(affected & suites.keys())
    ]
    return {
        "evaluations": evaluations,
        "uncovered_agents": sorted(affected - suites.keys()),
    }


def _number(value: Any) -> float:
    return float(value) if isinstance(value, int | float) else 0.0


def compare_scorecards(
    agent: str,
    baseline: dict[str, Any] | None,
    candidate: dict[str, Any],
) -> dict[str, Any]:
    safe_baseline = _mapping(scrub_structure(baseline or {}))
    safe_candidate = _mapping(scrub_structure(candidate))
    baseline_summary = _mapping(safe_baseline.get("summary"))
    candidate_summary = _mapping(safe_candidate.get("summary"))
    metrics: dict[str, dict[str, float]] = {}
    for name in ("pass_rate", "cost_usd", "p95_latency_ms"):
        before = _number(baseline_summary.get(name))
        after = _number(candidate_summary.get(name))
        metrics[name] = {"baseline": before, "candidate": after, "delta": round(after - before, 4)}

    baseline_cases = {
        str(item.get("name") or ""): bool(item.get("passed"))
        for item in baseline_summary.get("results", [])
        if isinstance(item, dict)
    }
    if not baseline_cases:
        baseline_cases = {
            str(item.get("name") or ""): bool(item.get("passed"))
            for item in safe_baseline.get("results", [])
            if isinstance(item, dict)
        }
    candidate_results = [item for item in safe_candidate.get("results", []) if isinstance(item, dict)]
    regressions = sorted(
        str(item.get("name") or "")
        for item in candidate_results
        if baseline_cases.get(str(item.get("name") or "")) is True and not bool(item.get("passed"))
    )
    newly_passing = sorted(
        str(item.get("name") or "")
        for item in candidate_results
        if baseline_cases.get(str(item.get("name") or "")) is False and bool(item.get("passed"))
    )
    has_baseline = bool(safe_baseline)
    status = "no-baseline"
    if has_baseline:
        status = (
            "regressed"
            if regressions or metrics["pass_rate"]["delta"] < 0
            else "improved"
            if newly_passing or metrics["pass_rate"]["delta"] > 0
            else "unchanged"
        )
    return {
        "agent": agent,
        "status": status,
        "candidate_run_id": str(safe_candidate.get("id") or ""),
        "metrics": metrics,
        "regressions": regressions,
        "newly_passing": newly_passing,
        "candidate_results": candidate_results,
    }


def render_comparison_markdown(comparison: dict[str, Any]) -> str:
    metrics = _mapping(comparison.get("metrics"))
    lines = [
        f"### `{comparison.get('agent', '')}` — {comparison.get('status', '')}",
        "",
        f"Candidate run: `{comparison.get('candidate_run_id') or 'not persisted'}`",
        "",
        "| Metric | Baseline | Candidate | Delta |",
        "|---|---:|---:|---:|",
    ]
    for name in ("pass_rate", "cost_usd", "p95_latency_ms"):
        values = _mapping(metrics.get(name))
        lines.append(
            f"| {name} | {values.get('baseline', 0)} | {values.get('candidate', 0)} | {values.get('delta', 0):+} |"
        )
    regressions = comparison.get("regressions") or []
    if regressions:
        lines.extend(("", "Regressions: " + ", ".join(f"`{item}`" for item in regressions)))
        results = comparison.get("candidate_results") or []
        for result in results:
            if not isinstance(result, dict) or result.get("name") not in regressions:
                continue
            failures = "; ".join(str(item) for item in result.get("failures") or [])
            trace = str(result.get("trace_url") or result.get("trace_id") or "")
            suffix = f" (trace: `{trace}`)" if trace else ""
            lines.append(f"- `{result.get('name')}`: {failures or 'failed'}{suffix}")
    return scrub("\n".join(lines) + "\n")


def _json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return {str(key): item for key, item in value.items()}


def _changed_paths(repo_root: Path, base: str, head: str) -> list[str]:
    completed = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...{head}", "--"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return [line for line in completed.stdout.splitlines() if line]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    impact = commands.add_parser("impact")
    impact.add_argument("--repo-root", type=Path, default=Path.cwd())
    impact.add_argument("--base", required=True)
    impact.add_argument("--head", required=True)
    impact.add_argument("--output", type=Path, required=True)

    compare = commands.add_parser("compare")
    compare.add_argument("--agent", required=True)
    compare.add_argument("--baseline", type=Path)
    compare.add_argument("--candidate", type=Path, required=True)
    compare.add_argument("--output", type=Path, required=True)
    compare.add_argument("--markdown", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "impact":
        result = resolve_impact(args.repo_root, _changed_paths(args.repo_root, args.base, args.head))
        args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return 0

    baseline = _json_object(args.baseline) if args.baseline and args.baseline.is_file() else None
    comparison = compare_scorecards(args.agent, baseline, _json_object(args.candidate))
    args.output.write_text(json.dumps(comparison, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    args.markdown.write_text(render_comparison_markdown(comparison), encoding="utf-8")
    return 2 if comparison["status"] == "regressed" else 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["compare_scorecards", "main", "render_comparison_markdown", "resolve_impact"]
