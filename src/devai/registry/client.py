"""HTTP client for the aregistry v0 catalog API.

One client per process. Constructed via :func:`create_registry_client`
from settings; never raises on construction so the rest of the app can
degrade gracefully when the catalog is unreachable.

Threading: methods are sync ``def`` (not ``async``). The FastAPI routes
that wrap this run the call in a threadpool via Starlette's default
behavior. The Python pipeline runtime is async — it calls these from
``asyncio.to_thread(...)`` (already the pattern used for the SCM
client).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class RegistryError(Exception):
    """Any failure reaching aregistry or parsing its response.

    Specialization loader catches this and falls back to local YAML so a
    transient registry blip never crashes the pipeline.
    """


# --------------------------------------------------------------------------- #
# Record dataclasses — the typed surface callers see
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Skill:
    name: str
    description: str
    version: str
    category: str = ""
    title: str = ""
    status: str = ""
    repository: dict[str, Any] | None = None
    packages: list[Any] = field(default_factory=list)
    remotes: list[Any] = field(default_factory=list)
    website_url: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Prompt:
    name: str
    version: str
    content: str
    description: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class McpServer:
    name: str
    type: str
    description: str = ""
    version: str = ""
    url: str = ""
    image: str = ""
    command: str = ""
    args: list[Any] = field(default_factory=list)
    env: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Agent:
    name: str
    description: str
    version: str
    image: str = ""
    language: str = ""
    framework: str = ""
    model_provider: str = ""
    model_name: str = ""
    skills: list[Any] = field(default_factory=list)
    prompts: list[Any] = field(default_factory=list)
    mcp_servers: list[Any] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #


class RegistryClient:
    """Cached HTTP client for aregistry v0.

    Cache is a tiny TTL map keyed by endpoint name. Cache is invalidated
    by :meth:`refresh`; the pipeline calls that after a publish.
    """

    def __init__(
        self,
        *,
        base_url: str,
        token: str = "",
        timeout_seconds: float = 5.0,
        ttl_seconds: float = 30.0,
    ) -> None:
        if not base_url:
            raise RegistryError("registry: base_url is required")
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout_seconds
        self._ttl = ttl_seconds
        self._cache: dict[str, tuple[float, Any]] = {}

    # ---- public API -------------------------------------------------------

    def health(self) -> dict[str, Any]:
        """Returns aregistry's reported health + a local 'reachable' flag.

        Health is the only call that bypasses the cache — every health
        probe should hit the network.
        """
        try:
            data = self._get("/v0/health", use_cache=False)
            return {"reachable": True, **data}
        except RegistryError as e:
            return {"reachable": False, "error": str(e)}

    def list_skills(self) -> list[Skill]:
        raw = self._get_collection("/v0/skills", "skills")
        return [_parse_skill(_unwrap(d, "skill")) for d in raw]

    def list_prompts(self) -> list[Prompt]:
        raw = self._get_collection("/v0/prompts", "prompts")
        return [_parse_prompt(_unwrap(d, "prompt")) for d in raw]

    def list_mcp_servers(self) -> list[McpServer]:
        # aregistry's endpoint is /v0/servers; Fiber/DevAI's tree calls
        # them "mcp-servers". The public client method uses the DevAI
        # name; the HTTP path is the registry's.
        raw = self._get_collection("/v0/servers", "servers")
        return [_parse_mcp_server(_unwrap(d, "server")) for d in raw]

    def list_agents(self) -> list[Agent]:
        raw = self._get_collection("/v0/agents", "agents")
        return [_parse_agent(_unwrap(d, "agent")) for d in raw]

    def get_skill(self, name: str) -> Skill | None:
        for s in self.list_skills():
            if s.name == name:
                return s
        return None

    def get_prompt(self, name: str) -> Prompt | None:
        for p in self.list_prompts():
            if p.name == name:
                return p
        return None

    def get_mcp_server(self, name: str) -> McpServer | None:
        for s in self.list_mcp_servers():
            if s.name == name:
                return s
        return None

    def get_agent(self, name: str) -> Agent | None:
        for a in self.list_agents():
            if a.name == name:
                return a
        return None

    def refresh(self, *keys: str) -> None:
        """Drop cached entries. With no args, drops everything."""
        if not keys:
            self._cache.clear()
            return
        for k in keys:
            self._cache.pop(k, None)

    def counts(self) -> dict[str, int]:
        """Single-call snapshot of catalog sizes for the dashboard."""
        return {
            "skills": len(self.list_skills()),
            "prompts": len(self.list_prompts()),
            "mcp_servers": len(self.list_mcp_servers()),
            "agents": len(self.list_agents()),
        }

    # ---- write surface ----------------------------------------------------

    def publish_skill(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._post("/v0/skills", body)

    def publish_prompt(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._post("/v0/prompts", body)

    def publish_mcp_server(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._post("/v0/servers", body)

    def publish_agent(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._post("/v0/agents", body)

    # ---- private ----------------------------------------------------------

    def _get(self, path: str, *, use_cache: bool = True) -> dict[str, Any]:
        if use_cache:
            cached = self._cache_get(path)
            if cached is not None:
                return cached
        data = self._request("GET", path, body=None)
        if use_cache:
            self._cache_set(path, data)
        return data

    def _get_collection(self, path: str, item_key: str) -> list[dict[str, Any]]:
        data = self._get(path)
        # aregistry returns {"<kind>": [...], "metadata": {"count": N}}.
        # Tolerate missing keys so the dashboard renders an empty list
        # rather than crashing if a future API trims the envelope.
        items = data.get(item_key) or []
        if not isinstance(items, list):
            logger.warning("registry: %s did not return a list under %r", path, item_key)
            return []
        return items

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", path, body=body)

    def _request(self, method: str, path: str, *, body: dict[str, Any] | None) -> dict[str, Any]:
        # Lazy-imported so a pod that never talks to the registry (e.g.
        # local unit tests) doesn't pay the httpx import cost.
        try:
            import httpx  # type: ignore[import-untyped]
        except ImportError as e:
            raise RegistryError(f"registry: httpx not installed: {e}") from e

        url = f"{self._base_url}{path}"
        headers: dict[str, str] = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            r = httpx.request(
                method,
                url,
                headers=headers,
                content=json.dumps(body) if body is not None else None,
                timeout=self._timeout,
            )
        except httpx.HTTPError as e:
            raise RegistryError(f"registry: network error {method} {path}: {e}") from e

        if r.status_code >= 500:
            raise RegistryError(f"registry: {r.status_code} on {method} {path}: {r.text[:200]}")
        if r.status_code >= 400 and method != "POST":
            raise RegistryError(f"registry: {r.status_code} on {method} {path}: {r.text[:200]}")

        if r.headers.get("content-type", "").startswith("application/json") and r.text:
            try:
                return r.json()
            except json.JSONDecodeError as e:
                raise RegistryError(f"registry: invalid JSON on {method} {path}: {e}") from e
        return {}

    def _cache_get(self, key: str) -> Any:
        hit = self._cache.get(key)
        if hit is None:
            return None
        expires_at, value = hit
        if expires_at < time.monotonic():
            self._cache.pop(key, None)
            return None
        return value

    def _cache_set(self, key: str, value: Any) -> None:
        self._cache[key] = (time.monotonic() + self._ttl, value)


# --------------------------------------------------------------------------- #
# Parsers — raw JSON to typed dataclasses
# --------------------------------------------------------------------------- #


def _unwrap(item: dict[str, Any], key: str) -> dict[str, Any]:
    """aregistry wraps each list entry in a {<kind>: {...}, _meta: ...}
    envelope. Return the inner record so the parsers see the flat
    schema they expect. Tolerates already-flat records too (older
    responses or future API changes)."""
    if not isinstance(item, dict):
        return {}
    inner = item.get(key)
    if isinstance(inner, dict):
        return inner
    return item


def _parse_skill(d: dict[str, Any]) -> Skill:
    return Skill(
        name=d.get("name", ""),
        description=d.get("description", ""),
        version=str(d.get("version", "")),
        category=d.get("category", "") or "",
        title=d.get("title", "") or "",
        status=d.get("status", "") or "",
        repository=d.get("repository"),
        packages=list(d.get("packages") or []),
        remotes=list(d.get("remotes") or []),
        website_url=d.get("websiteUrl", "") or "",
        raw=d,
    )


def _parse_prompt(d: dict[str, Any]) -> Prompt:
    return Prompt(
        name=d.get("name", ""),
        version=str(d.get("version", "")),
        content=d.get("content", "") or "",
        description=d.get("description", "") or "",
        raw=d,
    )


def _parse_mcp_server(d: dict[str, Any]) -> McpServer:
    return McpServer(
        name=d.get("name", ""),
        type=d.get("type", "") or "",
        description=d.get("description", "") or "",
        version=str(d.get("version", "")),
        url=d.get("url", "") or "",
        image=d.get("image", "") or "",
        command=d.get("command", "") or "",
        args=list(d.get("args") or []),
        env=dict(d.get("env") or {}),
        headers=dict(d.get("headers") or {}),
        raw=d,
    )


def _parse_agent(d: dict[str, Any]) -> Agent:
    return Agent(
        name=d.get("name", ""),
        description=d.get("description", "") or "",
        version=str(d.get("version", "")),
        image=d.get("image", "") or "",
        language=d.get("language", "") or "",
        framework=d.get("framework", "") or "",
        model_provider=d.get("modelProvider", "") or "",
        model_name=d.get("modelName", "") or "",
        skills=list(d.get("skills") or []),
        prompts=list(d.get("prompts") or []),
        mcp_servers=list(d.get("mcpServers") or []),
        raw=d,
    )


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #


def create_registry_client(settings: Any) -> RegistryClient | None:
    """Build a RegistryClient from settings. Never raises.

    Returns ``None`` when configuration is missing or the URL is empty —
    callers treat ``None`` as "registry not wired up" and fall back to
    local YAML.
    """
    base_url = getattr(settings, "registry_url", "") or ""
    if not base_url:
        logger.info("registry: DEVAI_REGISTRY_URL unset — client disabled")
        return None
    token = getattr(settings, "registry_token", "") or ""
    timeout = float(getattr(settings, "registry_timeout_seconds", 5.0) or 5.0)
    ttl = float(getattr(settings, "registry_cache_ttl_seconds", 30.0) or 30.0)
    try:
        return RegistryClient(
            base_url=base_url, token=token, timeout_seconds=timeout, ttl_seconds=ttl
        )
    except RegistryError as e:
        logger.warning("registry: client construction failed: %s", e)
        return None


# Re-export utility: lets a stage do `from devai.registry import iter_all`.
def iter_all(client: RegistryClient) -> Iterable[tuple[str, list[Any]]]:
    """Yield (kind, items) for every kind in one pass. Cheap with cache."""
    yield "skills", client.list_skills()
    yield "prompts", client.list_prompts()
    yield "mcp_servers", client.list_mcp_servers()
    yield "agents", client.list_agents()
