"""Settings capability — per-user/per-tenant connectors, preferences, secrets.

A connector is a configured integration (an LLM provider, an SCM, an MCP
server, Slack, …) scoped to a user, team, tenant, or the global default.
Non-secret fields (chosen provider, model, endpoints, the MCP server list) live
in Postgres; secret values live only in the secrets backend (GCP SM) and the
store keeps just a ``SecretRef``.

Resolution order when a connector is looked up for a Principal:

    user → team(s) → tenant → global

The resolved view is exposed as a ``PrincipalSettingsOverlay`` — a duck-typed
object the existing ``getattr(settings, ...)`` adapter factories accept, so a
user's own credentials transparently drive both their conversations and their
pipeline runs with no factory changes.
"""

from __future__ import annotations

from devai.settings.models import (
    CONNECTOR_SPECS,
    Connector,
    ConnectorField,
    ConnectorSpec,
    Scope,
)
from devai.settings.overlay import PrincipalSettingsOverlay
from devai.settings.service import SettingsService

__all__ = [
    "CONNECTOR_SPECS",
    "Connector",
    "ConnectorField",
    "ConnectorSpec",
    "PrincipalSettingsOverlay",
    "Scope",
    "SettingsService",
]
