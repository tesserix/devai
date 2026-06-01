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
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

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
    system_prompt: str = ""
    title: str = ""
    skills: list[Any] = field(default_factory=list)
    tools: list[Any] = field(default_factory=list)
    prompts: list[Any] = field(default_factory=list)
    mcp_servers: list[Any] = field(default_factory=list)
    a2a: dict[str, Any] = field(default_factory=dict)
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
        token_provider: Callable[[], str] | None = None,
        timeout_seconds: float = 5.0,
        ttl_seconds: float = 30.0,
        namespace: str = "",
    ) -> None:
        if not base_url:
            raise RegistryError("registry: base_url is required")
        self._base_url = base_url.rstrip("/")
        self._token = token
        # The tenant the platform's artifacts live under. aregistry scopes
        # list endpoints by ``?namespace=`` and returns an EMPTY list when it's
        # omitted, so the catalog (seeded under this namespace by the bootstrap)
        # is invisible unless we pass it. Empty → unscoped (registry default).
        self._namespace = (namespace or "").strip()
        # Per-request bearer resolver (OIDC client-credentials, self-caching).
        # Falls back to the static token when unset or it returns empty.
        self._token_provider = token_provider
        self._timeout = timeout_seconds
        self._ttl = ttl_seconds
        self._cache: dict[str, tuple[float, Any]] = {}

    def _bearer(self) -> str:
        if self._token_provider is not None:
            try:
                tok = self._token_provider()
                if tok:
                    return tok
            except Exception:  # noqa: BLE001 — never let auth break a read
                logger.warning("registry: token provider failed; falling back", exc_info=True)
        return self._token

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

    def _ns(self, path: str) -> str:
        """Append the configured tenant namespace to a list path. aregistry
        returns an empty list for an unscoped list call, so every catalog read
        must carry ``?namespace=``."""
        if not self._namespace:
            return path
        sep = "&" if "?" in path else "?"
        return f"{path}{sep}namespace={self._namespace}"

    def list_skills(self) -> list[Skill]:
        raw = self._get_collection(self._ns("/v0/skills"), "skills")
        return [_parse_skill(_unwrap(d, "skill")) for d in raw]

    def list_prompts(self) -> list[Prompt]:
        raw = self._get_collection(self._ns("/v0/prompts"), "prompts")
        return [_parse_prompt(_unwrap(d, "prompt")) for d in raw]

    def list_mcp_servers(self) -> list[McpServer]:
        # aregistry's endpoint is /v0/servers; Fiber/DevAI's tree calls
        # them "mcp-servers". The public client method uses the DevAI
        # name; the HTTP path is the registry's.
        raw = self._get_collection(self._ns("/v0/servers"), "servers")
        return [_parse_mcp_server(_unwrap(d, "server")) for d in raw]

    def list_agents(self) -> list[Agent]:
        raw = self._get_collection(self._ns("/v0/agents"), "agents")
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

    def get_agent_card(self, name: str, *, namespace: str = "", tag: str = "") -> dict[str, Any]:
        """Fetch the A2A (Agent2Agent) Agent Card for an agent.

        The registry renders the card from the Agent object + its linked
        registry Skills and returns a standards-compliant A2A descriptor —
        capabilities, the agent's own service ``url``, and ``skills``. The
        registry never proxies the agent; a consumer reads ``url`` from the
        card and talks A2A directly to it. ``tag`` pins a specific version;
        omitted resolves the latest. ``namespace`` scopes to an org/team.
        """
        path = f"/v0/agents/{name}/{tag}/card" if tag else f"/v0/agents/{name}/card"
        ns = namespace or self._namespace
        if ns:
            path += f"?namespace={ns}"
        return self._get(path)

    def get_signing_key(self) -> dict[str, Any]:
        """Fetch the registry's published Ed25519 signing public key.

        Returns ``{enabled, algorithm, keyId, publicKey, encoding, signs}``.
        ``enabled=false`` means the registry isn't attesting artifacts, so a
        consumer cannot verify card authenticity. Cached like other reads.
        """
        return self._get("/v0/signing-key")

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

    def publish_blueprint(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._post("/v0/blueprints", body)

    def publish_workflow(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._post("/v0/workflows", body)

    def publish_tool(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._post("/v0/tools", body)

    def delete(self, plural: str, name: str, tag: str = "latest") -> None:
        self._request("DELETE", f"/v0/{plural}/{name}/{tag}", body=None, raise_on_error=True)

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
        # aregistry's /v0 list returns a BARE JSON array of Kubernetes-style
        # envelopes. Older/compat responses may wrap as {"<kind>": [...]}.
        # Accept both so the dashboard never crashes on a shape change.
        if isinstance(data, list):
            items: Any = data
        elif isinstance(data, dict):
            items = data.get(item_key) or data.get("items") or []
        else:
            items = []
        if not isinstance(items, list):
            logger.warning("registry: %s did not return a list under %r", path, item_key)
            return []
        return items

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        # Publishes must surface 4xx (conflict/forbidden) — unlike read fallbacks.
        return self._request("POST", path, body=body, raise_on_error=True)

    def _request(
        self, method: str, path: str, *, body: dict[str, Any] | None, raise_on_error: bool = False
    ) -> dict[str, Any]:
        # Lazy-imported so a pod that never talks to the registry (e.g.
        # local unit tests) doesn't pay the httpx import cost.
        try:
            import httpx  # type: ignore[import-untyped]
        except ImportError as e:
            raise RegistryError(f"registry: httpx not installed: {e}") from e

        url = f"{self._base_url}{path}"
        headers: dict[str, str] = {"Accept": "application/json"}
        bearer = self._bearer()
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
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
        if r.status_code >= 400 and (raise_on_error or method != "POST"):
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
    """Flatten a catalog list entry into the flat record the parsers expect.

    aregistry returns Kubernetes-style envelopes
    ``{apiVersion, kind, metadata:{name,tag,...}, spec:{...}}``. We project
    ``metadata.name``/``tag`` and the spec fields up to the top level. Also
    tolerates the older ``{<kind>: {...}}`` wrap and already-flat records.
    """
    if not isinstance(item, dict):
        return {}
    inner = item.get(key)
    if isinstance(inner, dict):
        return inner
    spec = item.get("spec")
    if isinstance(spec, dict):
        meta = item.get("metadata") or {}
        flat: dict[str, Any] = {**spec}
        if isinstance(meta, dict):
            if meta.get("name") and "name" not in flat:
                flat["name"] = meta["name"]
            if meta.get("tag") and "version" not in flat:
                flat["version"] = meta["tag"]
        return flat
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
    # Composition shape: spec.model.{provider,name}. Legacy/seed shapes used
    # top-level modelProvider/modelName or an `llm` block — accept all.
    model = d.get("model") if isinstance(d.get("model"), dict) else {}
    llm = d.get("llm") if isinstance(d.get("llm"), dict) else {}
    provider = model.get("provider") or d.get("modelProvider") or llm.get("provider") or ""
    name_field = model.get("name") or d.get("modelName") or llm.get("model") or ""
    # The seed agents use spec.skill (singular) + spec.promptRef; the new
    # composition shape uses skills[]/prompts[]/mcpServers[].
    skills = d.get("skills")
    if skills is None and d.get("skill"):
        skills = [d["skill"]]
    prompts = d.get("prompts")
    if prompts is None and d.get("promptRef"):
        prompts = [d["promptRef"]]
    return Agent(
        name=d.get("name", ""),
        description=d.get("description", "") or "",
        version=str(d.get("version", "")),
        image=d.get("image", "") or "",
        language=d.get("language", "") or "",
        framework=d.get("framework", "") or "",
        model_provider=str(provider or ""),
        model_name=str(name_field or ""),
        system_prompt=d.get("systemPrompt", "") or "",
        title=d.get("title", "") or "",
        skills=list(skills or []),
        tools=list(d.get("tools") or []),
        prompts=list(prompts or []),
        mcp_servers=list(d.get("mcpServers") or []),
        a2a=dict(d.get("a2a") or {}),
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

    # Prefer an OIDC client-credentials provider (short-lived, self-caching,
    # scoped) for the WRITE path; the static token is the fallback for reads.
    token_provider: Callable[[], str] | None = None
    if getattr(settings, "registry_oidc_token_url", "") or getattr(settings, "registry_client_id", ""):
        try:
            from devai.adapters.registry.oidc import resolve_registry_token  # type: ignore

            token_provider = lambda: resolve_registry_token(settings) or token  # noqa: E731
        except Exception:  # noqa: BLE001
            logger.warning("registry: OIDC token provider unavailable; using static token", exc_info=True)

    try:
        # The tenant the catalog is seeded under (bootstrap uses `namespace:
        # devai`). aregistry list endpoints return empty without it.
        namespace = getattr(settings, "registry_default_tenant", "") or getattr(settings, "github_org", "") or "devai"
        return RegistryClient(
            base_url=base_url,
            token=token,
            token_provider=token_provider,
            timeout_seconds=timeout,
            ttl_seconds=ttl,
            namespace=namespace,
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
