"""The sandbox contract — an immutable, fully-pinned agent configuration.

A sandbox runs the *same* agent runtime as production with different boundaries.
Everything that could change a result is pinned here (agent version, model, prompt
version, tool modes, dataset version), because without that "did v17 improve?" has
no trustworthy answer.

Defaults are safe rather than convenient: a tool nobody classified is mocked, not
executed.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

# A sandbox is ephemeral by design; anything longer is a deployment.
MAX_TTL_SECONDS = 24 * 60 * 60
DEFAULT_TTL_SECONDS = 4 * 60 * 60


class ToolMode(str, enum.Enum):
    """What a tool call actually reaches inside a sandbox."""

    REAL = "real"
    MOCK = "mock"
    REPLAY = "replay"
    BLOCK = "block"


class SandboxStatus(str, enum.Enum):
    PENDING = "pending"
    PROVISIONING = "provisioning"
    READY = "ready"
    DESTROYING = "destroying"
    DESTROYED = "destroyed"
    FAILED = "failed"


class _Pinned(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class AgentRef(_Pinned):
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)


class ModelRef(_Pinned):
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)


class PromptRef(_Pinned):
    ref: str = Field(min_length=1)
    version: str = Field(min_length=1)


class DatasetRef(_Pinned):
    ref: str = Field(min_length=1)
    version: str = Field(min_length=1)


class RepoRef(_Pinned):
    """The repo the workspace starts as, at a commit that cannot move.

    Both fields reach a shell in the seed container, which holds the clone
    token, so the grammar is narrow rather than merely non-empty.
    """

    url: str = Field(min_length=1, pattern=r"^https://[A-Za-z0-9.\-]+(:\d+)?/[A-Za-z0-9._\-/]+$")
    ref: str = Field(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9._\-/]+$")
    scope: str = Field(min_length=1, pattern=r"^[A-Za-z0-9._\-]+/[A-Za-z0-9._\-]+$")


class ToolPolicy(_Pinned):
    default_mode: ToolMode = ToolMode.MOCK
    overrides: dict[str, ToolMode] = Field(default_factory=dict)

    def mode_for(self, tool: str) -> ToolMode:
        return self.overrides.get(tool, self.default_mode)


class SandboxLimits(_Pinned):
    max_tokens: int = Field(default=100_000, gt=0)
    max_cost_usd: float = Field(default=10.0, gt=0)
    max_wall_clock_s: int = Field(default=900, gt=0)


class SandboxCredentials(_Pinned):
    """Named user connectors explicitly granted to this sandbox.

    The connector is resolved in the control plane. Its secret is never
    mounted into the sandbox Job or workspace.
    """

    llm_connector: str = Field(default="", max_length=128, pattern=r"^$|^[A-Za-z0-9._-]+$")
    confirmed: bool = False

    @model_validator(mode="after")
    def connector_is_explicitly_confirmed(self) -> SandboxCredentials:
        if bool(self.llm_connector) != self.confirmed:
            raise ValueError("LLM connector and explicit confirmation must be supplied together")
        return self


class SandboxSpec(_Pinned):
    agent: AgentRef
    model: ModelRef
    prompt: PromptRef | None = None
    dataset: DatasetRef | None = None
    # The agent runtime release the run is built on. Unset at creation means the
    # newest offered one, and the service pins it before the record is stored.
    adk_version: str | None = None
    # An unpublished Agent envelope, tested here before it reaches the catalog.
    # Same shape a published agent has, so what passes is what ships.
    draft: dict[str, Any] | None = None
    tools: ToolPolicy = Field(default_factory=ToolPolicy)
    limits: SandboxLimits = Field(default_factory=SandboxLimits)
    credentials: SandboxCredentials = Field(default_factory=SandboxCredentials)
    ttl_seconds: int = Field(default=DEFAULT_TTL_SECONDS, gt=0, le=MAX_TTL_SECONDS)
    # A place to work — volume, shell and file service. Off by default: an eval
    # run that only needs an answer should not carry a PVC.
    workspace: bool = False
    # What the workspace starts as. Absent means an empty tree.
    repo: RepoRef | None = None
    # Run an IDE in the workspace pod so a person can take the run over.
    ide: bool = False
    # Run Chromium in the workspace, driven by agents over CDP and observable
    # by people through the authenticated noVNC proxy.
    browser: bool = False
    # Hosts this run may reach on top of the platform allowlist, e.g. a private
    # package index. Additions are recorded so "what could it reach" is answerable.
    allow_domains: list[str] = Field(default_factory=list)
    # Credential scopes (e.g. ``owner/repo``) this run may be granted. The agent
    # picks its own arguments at runtime, so the bound has to be declared here.
    allow_scopes: list[str] = Field(default_factory=list)

    # `model` shadows pydantic's protected namespace; the field name is part of
    # the published contract, so silence the warning rather than rename it.
    model_config = ConfigDict(frozen=True, extra="forbid", protected_namespaces=())

    @model_validator(mode="after")
    def browser_needs_workspace(self) -> SandboxSpec:
        if self.browser and not self.workspace:
            raise ValueError("browser requires workspace=true")
        return self


class SandboxRecord(BaseModel):
    id: str
    owner: str
    spec: SandboxSpec
    status: SandboxStatus
    created_at: datetime
    expires_at: datetime
    last_access_at: datetime | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "DEFAULT_TTL_SECONDS",
    "MAX_TTL_SECONDS",
    "AgentRef",
    "DatasetRef",
    "ModelRef",
    "PromptRef",
    "RepoRef",
    "SandboxLimits",
    "SandboxRecord",
    "SandboxSpec",
    "SandboxStatus",
    "ToolMode",
    "ToolPolicy",
]
