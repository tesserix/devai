"""Reference-aware validation for ADK artifact sets."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True, slots=True)
class ValidationFailure:
    path: Path
    reference: str
    message: str


def validate_artifacts(
    files: Iterable[Path],
    *,
    deep: bool = False,
    catalog_roots: Iterable[Path] = (),
) -> list[ValidationFailure]:
    target_files = list(files)
    target_paths = {path.resolve() for path in target_files}
    documents: list[tuple[Path, dict[str, Any]]] = []
    seen_paths: set[Path] = set()
    for path in [*target_files, *_catalog_files(catalog_roots)]:
        resolved = path.resolve()
        if resolved in seen_paths:
            continue
        seen_paths.add(resolved)
        try:
            value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        if isinstance(value, dict):
            documents.append((path, _mapping(value)))
    if not deep:
        return []

    catalog: set[tuple[str, str, str]] = set()
    latest: set[tuple[str, str]] = set()
    for _, document in documents:
        kind = str(document.get("kind") or "")
        spec = _mapping(document.get("spec"))
        metadata = _mapping(document.get("metadata"))
        name = str(metadata.get("name") or spec.get("name") or "")
        version = str(spec.get("version") or metadata.get("tag") or "")
        if kind and name:
            latest.add((kind, name))
            catalog.add((kind, name, version))

    failures: list[ValidationFailure] = []
    for path, document in documents:
        if path.resolve() not in target_paths:
            continue
        for kind, name, version in _references(document):
            exists = (kind, name, version) in catalog if version else (kind, name) in latest
            if exists:
                continue
            reference = f"{kind}/{name}" + (f"@{version}" if version else "")
            failures.append(ValidationFailure(path, reference, f"unresolved reference {reference}"))
    return failures


def _catalog_files(roots: Iterable[Path]) -> list[Path]:
    found: list[Path] = []
    for root in roots:
        if root.is_file():
            found.append(root)
        elif root.is_dir():
            found.extend(root.rglob("*.yaml"))
            found.extend(root.rglob("*.yml"))
    return found


def _references(document: dict[str, Any]) -> list[tuple[str, str, str]]:
    kind = str(document.get("kind") or "")
    spec = _mapping(document.get("spec"))
    references: list[tuple[str, str, str]] = []
    if kind == "Agent":
        _add(references, "Skill", spec.get("skill"))
        for value in spec.get("skills") or []:
            _add(references, "Skill", value)
        _add(references, "Prompt", spec.get("promptRef"))
        for value in spec.get("mcpServers") or []:
            _add(references, "MCPServer", value)
        for value in spec.get("tools") or []:
            _add(references, "Tool", _reference_name(value))
        sandbox = _mapping(spec.get("sandbox"))
        dataset = _mapping(sandbox.get("dataset"))
        _add(references, "Dataset", dataset.get("ref"), dataset.get("version"))
    elif kind == "Skill":
        for value in spec.get("tools") or []:
            _add(references, "Tool", _reference_name(value))
    elif kind == "EvalSuite":
        dataset = _mapping(spec.get("datasetRef"))
        _add(references, "Dataset", dataset.get("ref"), dataset.get("version"))
    return references


def _reference_name(value: Any) -> Any:
    if isinstance(value, dict):
        return value.get("name") or value.get("ref")
    return value


def _mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}


def _add(target: list[tuple[str, str, str]], kind: str, name: Any, version: Any = "") -> None:
    if name:
        target.append((kind, str(name), str(version or "")))


__all__ = ["ValidationFailure", "validate_artifacts"]
