"""DevAI SDK entry point.

One ``DevAI`` object groups three sub-clients (registry / pipeline /
specializations) so consumers only construct one thing. Sub-clients
are lazily built on first access so an SDK user that only browses the
catalog never pays for the pipeline subsystem.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from devai.registry import (
    Agent,
    McpServer,
    Prompt,
    RegistryClient,
    RegistryError,
    Skill,
)

logger = logging.getLogger(__name__)


class DevAIError(Exception):
    """Top-level SDK error. Wraps registry / API failures."""


@dataclass(frozen=True, slots=True)
class CatalogView:
    """Snapshot of the catalog (one network round-trip per kind)."""

    skills: list[Skill]
    prompts: list[Prompt]
    mcp_servers: list[McpServer]
    agents: list[Agent]


class _IndexedList(list):
    """list[T] with ``.by_name(name)`` lookup. Used so SDK callers can
    say ``devai.agents().by_name('ci-monitor-agent')`` without an
    explicit loop. Subclasses ``list`` so iteration / len / index all
    keep working unchanged."""

    def by_name(self, name: str) -> Any | None:
        for item in self:
            if getattr(item, "name", "") == name:
                return item
        return None


class DevAI:
    """The DevAI SDK entry point.

    Construction is cheap and never raises. Methods either return a
    typed value or raise :class:`DevAIError` so callers don't have to
    pattern-match low-level HTTP exceptions.

    Parameters
    ----------
    registry_url:
        Base URL for the aregistry catalog. Empty string disables the
        registry sub-client (every catalog method returns an empty list
        with a warning logged).
    registry_token:
        Optional bearer token for aregistry. Empty when anonymous-auth
        is enabled (today's prod config).
    api_url:
        Base URL for the DevAI webhook + dashboard API (``/api/*``).
        Empty disables pipeline + specializations sub-clients.
    timeout_seconds:
        HTTP timeout for both clients. Defaults to 5s.
    """

    def __init__(
        self,
        *,
        registry_url: str = "",
        registry_token: str = "",
        api_url: str = "",
        timeout_seconds: float = 5.0,
    ) -> None:
        self._registry_url = registry_url.rstrip("/")
        self._registry_token = registry_token
        self._api_url = api_url.rstrip("/")
        self._timeout = timeout_seconds
        self._registry: RegistryClient | None = None
        self._pipeline = None  # type: ignore[var-annotated]
        self._specializations = None  # type: ignore[var-annotated]

    # ---- sub-clients (lazy) ------------------------------------------------

    @property
    def registry(self) -> RegistryClient | None:
        if self._registry is None and self._registry_url:
            try:
                self._registry = RegistryClient(
                    base_url=self._registry_url,
                    token=self._registry_token,
                    timeout_seconds=self._timeout,
                )
            except RegistryError as e:
                logger.warning("sdk: registry client construction failed: %s", e)
        return self._registry

    @property
    def pipeline(self):  # -> PipelineSDK
        if self._pipeline is None and self._api_url:
            from devai.sdk.pipeline import PipelineSDK

            self._pipeline = PipelineSDK(
                base_url=self._api_url, timeout_seconds=self._timeout
            )
        return self._pipeline

    @property
    def specializations(self):  # -> SpecializationsSDK
        if self._specializations is None and self._api_url:
            from devai.sdk.specializations import SpecializationsSDK

            self._specializations = SpecializationsSDK(
                base_url=self._api_url, timeout_seconds=self._timeout
            )
        return self._specializations

    # ---- catalog (registry passthroughs) ----------------------------------

    def skills(self) -> _IndexedList:
        return _IndexedList(self._call(lambda r: r.list_skills(), kind="skills"))

    def prompts(self) -> _IndexedList:
        return _IndexedList(self._call(lambda r: r.list_prompts(), kind="prompts"))

    def mcp_servers(self) -> _IndexedList:
        return _IndexedList(self._call(lambda r: r.list_mcp_servers(), kind="mcp servers"))

    def agents(self) -> _IndexedList:
        return _IndexedList(self._call(lambda r: r.list_agents(), kind="agents"))

    def skill(self, name: str) -> Skill | None:
        return self._call(lambda r: r.get_skill(name), kind=f"skill {name}")

    def prompt(self, name: str) -> Prompt | None:
        return self._call(lambda r: r.get_prompt(name), kind=f"prompt {name}")

    def mcp_server(self, name: str) -> McpServer | None:
        return self._call(lambda r: r.get_mcp_server(name), kind=f"mcp server {name}")

    def agent(self, name: str) -> Agent | None:
        return self._call(lambda r: r.get_agent(name), kind=f"agent {name}")

    def catalog(self) -> CatalogView:
        """One-shot snapshot of every kind. Convenient for dashboards
        that render all four panes from one fetch."""
        r = self.registry
        if r is None:
            return CatalogView(skills=[], prompts=[], mcp_servers=[], agents=[])
        try:
            return CatalogView(
                skills=r.list_skills(),
                prompts=r.list_prompts(),
                mcp_servers=r.list_mcp_servers(),
                agents=r.list_agents(),
            )
        except RegistryError as e:
            raise DevAIError(f"catalog fetch failed: {e}") from e

    # ---- catalog (write) --------------------------------------------------

    def publish_skill(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._call(lambda r: r.publish_skill(body), kind="publish skill")

    def publish_prompt(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._call(lambda r: r.publish_prompt(body), kind="publish prompt")

    def publish_mcp_server(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._call(lambda r: r.publish_mcp_server(body), kind="publish mcp server")

    def publish_agent(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._call(lambda r: r.publish_agent(body), kind="publish agent")

    def health(self) -> dict[str, Any]:
        r = self.registry
        if r is None:
            return {"registry": {"reachable": False, "error": "client not configured"}}
        return {"registry": r.health()}

    # ---- helpers ----------------------------------------------------------

    def _call(self, fn, *, kind: str):
        r = self.registry
        if r is None:
            logger.info("sdk: registry not configured — returning empty for %s", kind)
            return [] if "skill" in kind or "agent" in kind or "prompt" in kind or "server" in kind else None
        try:
            return fn(r)
        except RegistryError as e:
            raise DevAIError(f"{kind}: {e}") from e
