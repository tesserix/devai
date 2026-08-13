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

from pydantic import BaseModel, ConfigDict, Field

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


class ToolPolicy(_Pinned):
    default_mode: ToolMode = ToolMode.MOCK
    overrides: dict[str, ToolMode] = Field(default_factory=dict)

    def mode_for(self, tool: str) -> ToolMode:
        return self.overrides.get(tool, self.default_mode)


class SandboxLimits(_Pinned):
    max_tokens: int = Field(default=100_000, gt=0)
    max_cost_usd: float = Field(default=10.0, gt=0)
    max_wall_clock_s: int = Field(default=900, gt=0)


class SandboxSpec(_Pinned):
    agent: AgentRef
    model: ModelRef
    prompt: PromptRef | None = None
    dataset: DatasetRef | None = None
    tools: ToolPolicy = Field(default_factory=ToolPolicy)
    limits: SandboxLimits = Field(default_factory=SandboxLimits)
    ttl_seconds: int = Field(default=DEFAULT_TTL_SECONDS, gt=0, le=MAX_TTL_SECONDS)
    # A place to work — volume, shell and file service. Off by default: an eval
    # run that only needs an answer should not carry a PVC.
    workspace: bool = False

    # `model` shadows pydantic's protected namespace; the field name is part of
    # the published contract, so silence the warning rather than rename it.
    model_config = ConfigDict(frozen=True, extra="forbid", protected_namespaces=())


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
    "SandboxLimits",
    "SandboxRecord",
    "SandboxSpec",
    "SandboxStatus",
    "ToolMode",
    "ToolPolicy",
]
