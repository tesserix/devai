"""Fluent builders for aregistry entries.

Each builder mirrors a kind in the catalog (``Skill``, ``Agent``,
``McpServer``, ``Prompt``) and produces a JSON dict matching aregistry's
HTTP API exactly. Constraints discovered live during the bootstrap
run (name pattern for MCP, ``$schema`` for MCP, ``version: str``
everywhere) are encoded here so callers can't accidentally produce a
body the registry will reject.

The builders intentionally don't carry the rich orchestrator metadata
(``tools``, ``handoverSchema``, ``contextKeys``, …) that lives on the
local-YAML seeds — those stay on disk for the runtime layer to read.
The ADK's job is to make catalog entries round-trip cleanly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_MCP_NAME_RE = re.compile(r"^[a-zA-Z0-9.-]+/[a-zA-Z0-9._-]+$")
_MCP_SCHEMA_URL = (
    "https://modelcontextprotocol.io/schemas/draft/2025-07-09/server.schema.json"
)
_MCP_DESCRIPTION_MAX = 100


def _truncate(s: str, n: int) -> str:
    s = (s or "").strip()
    if len(s) <= n:
        return s
    return s[: n - 3].rstrip() + "..."


@dataclass(slots=True)
class Skill:
    """A skill row in the catalog.

    Required fields per :class:`SkillJSON`: ``name``, ``description``,
    ``version``. The builder defaults ``version="1"`` so a one-liner
    works. Call :meth:`to_dict` to get the wire body.
    """

    _name: str
    _description: str = ""
    _version: str = "1"
    _category: str = ""
    _title: str = ""
    _status: str = ""
    _repository: dict[str, Any] | None = None
    _packages: list[Any] = field(default_factory=list)
    _remotes: list[Any] = field(default_factory=list)
    _website_url: str = ""

    def description(self, text: str) -> "Skill":
        self._description = text
        return self

    def version(self, v: str) -> "Skill":
        self._version = str(v)
        return self

    def category(self, c: str) -> "Skill":
        self._category = c
        return self

    def title(self, t: str) -> "Skill":
        self._title = t
        return self

    def status(self, s: str) -> "Skill":
        self._status = s
        return self

    def repository(self, url: str, *, kind: str = "git") -> "Skill":
        self._repository = {"url": url, "type": kind}
        return self

    def website(self, url: str) -> "Skill":
        self._website_url = url
        return self

    @property
    def name(self) -> str:
        return self._name

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "name": self._name,
            "description": self._description or self._name,
            "version": str(self._version),
        }
        if self._category:
            body["category"] = self._category
        if self._title:
            body["title"] = self._title
        if self._status:
            body["status"] = self._status
        if self._repository:
            body["repository"] = self._repository
        if self._packages:
            body["packages"] = self._packages
        if self._remotes:
            body["remotes"] = self._remotes
        if self._website_url:
            body["websiteUrl"] = self._website_url
        return body


@dataclass(slots=True)
class Prompt:
    _name: str
    _version: str = "1"
    _content: str = ""
    _description: str = ""

    def version(self, v: str) -> "Prompt":
        self._version = str(v)
        return self

    def content(self, text: str) -> "Prompt":
        self._content = text
        return self

    def description(self, text: str) -> "Prompt":
        self._description = text
        return self

    @property
    def name(self) -> str:
        return self._name

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self._name,
            "version": str(self._version),
            "content": self._content,
            "description": self._description,
        }


@dataclass(slots=True)
class McpServer:
    """An MCP server entry. The registry enforces the upstream MCP
    JSON schema, which differs from the OpenAPI shape — the builder
    handles the gotchas (publisher/server name pattern, $schema URL,
    description ≤ 100 chars)."""

    _name: str
    _description: str = ""
    _version: str = "1"
    _type: str = "remote"
    _url: str = ""
    _image: str = ""
    _command: str = ""
    _args: list[Any] = field(default_factory=list)
    _env: dict[str, Any] = field(default_factory=dict)
    _headers: dict[str, Any] = field(default_factory=dict)

    def description(self, text: str) -> "McpServer":
        self._description = text
        return self

    def version(self, v: str) -> "McpServer":
        self._version = str(v)
        return self

    def remote(self, url: str) -> "McpServer":
        self._type = "remote"
        self._url = url
        return self

    def container(self, image: str, *, args: list[str] | None = None) -> "McpServer":
        self._type = "container"
        self._image = image
        if args:
            self._args = list(args)
        return self

    def env(self, **kv: Any) -> "McpServer":
        self._env.update(kv)
        return self

    def headers(self, **kv: Any) -> "McpServer":
        self._headers.update(kv)
        return self

    @property
    def name(self) -> str:
        return self._name

    def to_dict(self) -> dict[str, Any]:
        name = self._name
        if not _MCP_NAME_RE.match(name):
            # Default publisher when the caller passed a bare name.
            name = f"devai/{name}"
        body: dict[str, Any] = {
            "$schema": _MCP_SCHEMA_URL,
            "name": name,
            "type": self._type,
            "version": str(self._version),
            "description": _truncate(self._description or self._name, _MCP_DESCRIPTION_MAX),
        }
        if self._url:
            body["url"] = self._url
        if self._image:
            body["image"] = self._image
        if self._command:
            body["command"] = self._command
        if self._args:
            body["args"] = self._args
        if self._env:
            body["env"] = self._env
        if self._headers:
            body["headers"] = self._headers
        return body


@dataclass(slots=True)
class Agent:
    _name: str
    _description: str = ""
    _version: str = "1"
    _image: str = "ghcr.io/tesserix/devai/devai:latest"
    _language: str = "python"
    _framework: str = "langgraph"
    _model_provider: str = "anthropic"
    _model_name: str = "claude-sonnet-4-20250514"
    _skills: list[str] = field(default_factory=list)
    _prompts: list[str] = field(default_factory=list)
    _mcp_servers: list[str] = field(default_factory=list)
    _title: str = ""
    _status: str = ""
    _website_url: str = ""

    def description(self, text: str) -> "Agent":
        self._description = text
        return self

    def version(self, v: str) -> "Agent":
        self._version = str(v)
        return self

    def image(self, img: str) -> "Agent":
        self._image = img
        return self

    def language(self, lang: str) -> "Agent":
        self._language = lang
        return self

    def framework(self, fw: str) -> "Agent":
        self._framework = fw
        return self

    def model(self, provider: str, model: str) -> "Agent":
        self._model_provider = provider
        self._model_name = model
        return self

    def skill(self, *names: str) -> "Agent":
        for n in names:
            if n and n not in self._skills:
                self._skills.append(n)
        return self

    def prompt(self, *names: str) -> "Agent":
        for n in names:
            if n and n not in self._prompts:
                self._prompts.append(n)
        return self

    def mcp_server(self, *names: str) -> "Agent":
        for n in names:
            if n and n not in self._mcp_servers:
                self._mcp_servers.append(n)
        return self

    def title(self, t: str) -> "Agent":
        self._title = t
        return self

    def status(self, s: str) -> "Agent":
        self._status = s
        return self

    def website(self, url: str) -> "Agent":
        self._website_url = url
        return self

    @property
    def name(self) -> str:
        return self._name

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "name": self._name,
            "description": self._description or self._name,
            "version": str(self._version),
            "image": self._image,
            "language": self._language,
            "framework": self._framework,
            "modelProvider": self._model_provider,
            "modelName": self._model_name,
        }
        if self._skills:
            body["skills"] = list(self._skills)
        if self._prompts:
            body["prompts"] = list(self._prompts)
        if self._mcp_servers:
            body["mcpServers"] = list(self._mcp_servers)
        if self._title:
            body["title"] = self._title
        if self._status:
            body["status"] = self._status
        if self._website_url:
            body["websiteUrl"] = self._website_url
        return body
