"""Thin consumer of Agent Registry's native semantic discovery index.

Agent Registry owns the safe document projection, pgvector embeddings, and
RBAC-first ranking. DevAI only adapts authorized discovery stubs for its HTTP
and MCP surfaces and applies its owner-label policy as defense in depth.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote, urlencode

from devai.identity import Principal

OWNER_LABEL = "devai.tesserix.app/owner-id"
CAPABILITY_KINDS = (
    "tools",
    "skills",
    "agents",
    "mcp_servers",
    "prompts",
    "workflows",
    "blueprints",
    "datasets",
    "eval_suites",
)

_REGISTRY_KIND = {
    "tool": "tools",
    "skill": "skills",
    "agent": "agents",
    "mcpserver": "mcp_servers",
    "prompt": "prompts",
    "workflow": "workflows",
    "blueprint": "blueprints",
    "dataset": "datasets",
    "evalsuite": "eval_suites",
}

_API_PLURAL = {
    "tools": "tools",
    "skills": "skills",
    "agents": "agents",
    "mcp_servers": "mcp-servers",
    "prompts": "prompts",
    "workflows": "workflows",
    "blueprints": "blueprints",
    "datasets": "datasets",
    "eval_suites": "eval-suites",
}


class RegistrySearchClient(Protocol):
    def search_capabilities(
        self,
        query: str,
        *,
        kinds: Iterable[str] | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]: ...


@dataclass(slots=True, frozen=True)
class SemanticSearchHit:
    kind: str
    name: str
    title: str
    description: str
    version: str
    namespace: str
    arn: str
    digest: str
    visibility: str
    labels: dict[str, str]
    annotations: dict[str, str]
    attributes: dict[str, Any]
    rank: int
    match_type: str
    fetch_path: str
    registry_fetch_path: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "version": self.version,
            "namespace": self.namespace,
            "arn": self.arn,
            "digest": self.digest,
            "visibility": self.visibility,
            "labels": dict(self.labels),
            "annotations": dict(self.annotations),
            "attributes": dict(self.attributes),
            "rank": self.rank,
            "match_type": self.match_type,
            "fetch_path": self.fetch_path,
            "registry_fetch_path": self.registry_fetch_path,
        }


@dataclass(slots=True, frozen=True)
class SemanticSearchResponse:
    query: str
    hits: list[SemanticSearchHit]
    provider: str = "agentic-registry"
    index_refreshing: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "hits": [hit.to_dict() for hit in self.hits],
            "provider": self.provider,
            "index_refreshing": self.index_refreshing,
        }


class RegistrySemanticSearch:
    """Delegate semantic discovery to the authoritative Agent Registry."""

    def __init__(self, registry: RegistrySearchClient) -> None:
        self._registry = registry

    async def search(
        self,
        query: str,
        *,
        principal: Principal | None = None,
        kinds: Sequence[str] | None = None,
        limit: int = 10,
    ) -> SemanticSearchResponse:
        normalized_query = query.strip()
        if not normalized_query:
            raise ValueError("query must not be empty")
        if len(normalized_query) > 512:
            raise ValueError("query must be at most 512 characters")
        if not 1 <= limit <= 50:
            raise ValueError("limit must be between 1 and 50")
        selected_kinds = _normalize_kinds(kinds)

        raw_hits = await asyncio.to_thread(
            self._registry.search_capabilities,
            normalized_query,
            kinds=selected_kinds,
            # Agent Registry authenticates the service identity; DevAI also
            # enforces its per-user owner label, so request spare candidates.
            limit=min(50, max(limit * 3, limit)),
        )
        hits: list[SemanticSearchHit] = []
        for raw in raw_hits:
            hit = _hit(raw, rank=len(hits) + 1)
            if hit is None or hit.kind not in selected_kinds or not _visible(hit, principal):
                continue
            hits.append(hit)
            if len(hits) >= limit:
                break
        return SemanticSearchResponse(query=normalized_query, hits=hits)


def principal_owner_id(principal: Principal | None) -> str:
    if principal is None:
        return ""
    scope = principal.user_scope_id.strip()
    if not scope:
        return ""
    return hashlib.sha256(scope.encode()).hexdigest()[:32]


def _normalize_kinds(kinds: Sequence[str] | None) -> tuple[str, ...]:
    if not kinds:
        return CAPABILITY_KINDS
    selected: list[str] = []
    for raw in kinds:
        normalized = raw.strip().lower().replace("-", "_")
        if normalized == "mcpservers":
            normalized = "mcp_servers"
        elif normalized == "evalsuites":
            normalized = "eval_suites"
        if normalized not in CAPABILITY_KINDS:
            raise ValueError(f"unsupported registry capability kind: {raw}")
        if normalized not in selected:
            selected.append(normalized)
    return tuple(selected)


def _hit(raw: dict[str, Any], *, rank: int) -> SemanticSearchHit | None:
    raw_kind = str(raw.get("kind") or "").lower().replace("-", "").replace("_", "")
    kind = _REGISTRY_KIND.get(raw_kind, "")
    name = str(raw.get("name") or "").strip()
    if not kind or not name:
        return None
    version = str(raw.get("tag") or "")
    attributes = raw.get("attributes")
    return SemanticSearchHit(
        kind=kind,
        name=name,
        title=str(raw.get("title") or name),
        description=str(raw.get("description") or ""),
        version=version,
        namespace=str(raw.get("namespace") or ""),
        arn=str(raw.get("arn") or ""),
        digest=str(raw.get("digest") or ""),
        visibility=str(raw.get("visibility") or ""),
        labels=_string_map(raw.get("labels")),
        annotations=_string_map(raw.get("annotations")),
        attributes=dict(attributes) if isinstance(attributes, dict) else {},
        rank=rank,
        match_type="semantic",
        fetch_path=_gateway_fetch_path(kind, name, version),
        registry_fetch_path=str(raw.get("fetchPath") or ""),
    )


def _visible(hit: SemanticSearchHit, principal: Principal | None) -> bool:
    owner = hit.labels.get(OWNER_LABEL, "")
    if owner:
        return owner == principal_owner_id(principal)
    return hit.visibility.lower() != "private"


def _gateway_fetch_path(kind: str, name: str, version: str) -> str:
    path = f"/api/registry/artifacts/{_API_PLURAL[kind]}/{quote(name, safe='')}"
    if version and version != "latest":
        path += f"?{urlencode({'tag': version})}"
    return path


def _string_map(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items() if isinstance(key, str) and isinstance(item, str)}
