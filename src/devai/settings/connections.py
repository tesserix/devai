"""Per-user connection resolution — connectors + their secrets, server-side only.

Settings stores a user's infrastructure connections (Kubernetes clusters,
cloud accounts, Argo CD / Kargo instances, MCP servers) as multi-instance
connectors: prefs in Postgres, secret VALUES in GCP Secret Manager under
the owner's scope. This module is the one place that joins the two back
together for in-process consumers (tools, adapters, MCP domains).

Hard rules:
  - NEVER expose resolved values over HTTP. Routes return ``secrets_set``
    (key names) only; this module is for code that acts on the user's
    behalf inside the platform.
  - Resolution is principal-scoped: a user's connections come from the
    user→team→tenant scopes that user can read, nothing else. Tenant
    isolation is the storage key, not a filter bolted on after.
  - Degrade, don't raise: a missing secret resolves to "" and the caller
    decides; a missing service yields [].
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from devai.identity import Principal
    from devai.settings.service import SettingsService

logger = logging.getLogger(__name__)


async def user_connections(
    identity: Principal | str, connector_key: str, *, svc: SettingsService | None = None
) -> list[dict[str, Any]]:
    """Every ``connector_key`` connection the user owns, secrets RESOLVED.

    Returns ``[{instance_id, provider, <prefs...>, <secret fields...>}]``,
    newest definition wins per instance. Empty list when the user has none
    or the settings backend is unavailable.
    """
    from devai.identity import Principal
    from devai.settings.models import Scope

    principal = identity if isinstance(identity, Principal) else Principal(email=str(identity or ""))
    if not principal.email or "@" not in principal.email:
        return []
    if svc is None:
        from devai.settings.service import get_settings_service

        svc = get_settings_service()
    if svc is None:
        return []
    try:
        lookups: list[tuple[Scope, str]] = [(Scope.GLOBAL, "")]
        if principal.tenant_id:
            lookups.append((Scope.TENANT, principal.tenant_id))
        lookups.extend((Scope.ORG, org_id) for org_id in principal.org_ids)
        lookups.extend((Scope.TEAM, team_id) for team_id in principal.team_ids)
        lookups.append((Scope.USER, principal.user_scope_id))
        merged = {}
        for scope, scope_id in lookups:
            for connector in await svc.list_connectors(scope, scope_id):
                if connector.enabled:
                    merged[(connector.connector_key, connector.instance_id)] = connector
        if not principal.tenant_id and hasattr(svc, "list_user_connectors_by_email"):
            for connector in await svc.list_user_connectors_by_email(principal.email):
                if connector.enabled:
                    merged[(connector.connector_key, connector.instance_id)] = connector
        connectors = list(merged.values())
    except Exception:  # noqa: BLE001 — settings outage must not break a tool call
        logger.warning("connections: list failed for %s", principal.user_scope_id, exc_info=True)
        return []
    out: list[dict[str, Any]] = []
    for c in connectors:
        if c.connector_key != connector_key or not c.enabled:
            continue
        resolved: dict[str, Any] = {"instance_id": c.instance_id, "provider": c.provider, **dict(c.prefs)}
        for field_key, ref in c.secret_refs.items():
            try:
                value = await svc.resolve_secret(ref)
            except Exception:  # noqa: BLE001
                value = None
            resolved[field_key] = value or ""
        out.append(resolved)
    return out


def _named(conns: list[dict[str, Any]], name: str, *name_fields: str) -> dict[str, Any] | None:
    """Pick a connection by instance_id or its human name field."""
    for c in conns:
        if c.get("instance_id") == name or any(c.get(f) == name for f in name_fields):
            return c
    return None


async def user_cluster(
    identity: Principal | str, name: str = "", *, svc: SettingsService | None = None
) -> dict[str, Any] | None:
    """One of the user's Kubernetes clusters as a kubectl-ready dict.

    Shape: ``{name, server, token, ca_data, namespace}``. ``name`` blank →
    the user's only cluster (None when they have zero or several — the
    caller should ask which).
    """
    conns = await user_connections(identity, "kubernetes", svc=svc)
    if not conns:
        return None
    chosen = _named(conns, name, "k8s_name") if name else (conns[0] if len(conns) == 1 else None)
    if chosen is None:
        return None
    return {
        "name": chosen.get("k8s_name") or chosen.get("instance_id", ""),
        "server": chosen.get("k8s_api_server", ""),
        "token": chosen.get("k8s_token", ""),
        "ca_data": chosen.get("k8s_ca_cert", ""),
        "namespace": chosen.get("k8s_namespace", ""),
    }


async def user_cluster_names(identity: Principal | str, *, svc: SettingsService | None = None) -> list[str]:
    conns = await user_connections(identity, "kubernetes", svc=svc)
    return [str(c.get("k8s_name") or c.get("instance_id", "")) for c in conns]


async def user_argocd(
    identity: Principal | str, name: str = "", *, svc: SettingsService | None = None
) -> dict[str, Any] | None:
    """One of the user's connected Argo CD instances (Settings → Argo CD).

    Shape: ``{name, mode, server_url, token, app_namespace}``. ``name`` blank →
    the user's only Argo CD (None when zero or several)."""
    conns = await user_connections(identity, "argocd", svc=svc)
    if not conns:
        return None
    chosen = _named(conns, name, "argocd_name") if name else (conns[0] if len(conns) == 1 else None)
    if chosen is None:
        return None
    return {
        "name": chosen.get("argocd_name") or chosen.get("instance_id", ""),
        "mode": chosen.get("provider", "api"),
        "server_url": chosen.get("argocd_server_url", ""),
        "token": chosen.get("argocd_token", ""),
        "app_namespace": chosen.get("argocd_app_namespace", ""),
    }


async def user_kargo(
    identity: Principal | str, name: str = "", *, svc: SettingsService | None = None
) -> dict[str, Any] | None:
    """One of the user's connected Kargo control planes (Settings → Kargo).

    Shape: ``{name, mode, api_url, token, project}``."""
    conns = await user_connections(identity, "kargo", svc=svc)
    if not conns:
        return None
    chosen = _named(conns, name, "kargo_name") if name else (conns[0] if len(conns) == 1 else None)
    if chosen is None:
        return None
    return {
        "name": chosen.get("kargo_name") or chosen.get("instance_id", ""),
        "mode": chosen.get("provider", "api"),
        "api_url": chosen.get("kargo_api_url", ""),
        "token": chosen.get("kargo_token", ""),
        "project": chosen.get("kargo_project", ""),
    }


async def user_cloud(
    identity: Principal | str, name: str = "", *, svc: SettingsService | None = None
) -> dict[str, Any] | None:
    """One of the user's connected cloud accounts (Settings → Cloud Account).

    Returns the resolved connector dict (provider + creds joined from SM),
    ready for ``adapters.cloud.create_cloud_adapter``. ``name`` blank → the
    user's only account (None when zero or several)."""
    conns = await user_connections(identity, "cloud", svc=svc)
    if not conns:
        return None
    return _named(conns, name, "cloud_name") if name else (conns[0] if len(conns) == 1 else None)


async def user_cloud_names(identity: Principal | str, *, svc: SettingsService | None = None) -> list[str]:
    conns = await user_connections(identity, "cloud", svc=svc)
    return [str(c.get("cloud_name") or c.get("instance_id", "")) for c in conns]


__all__ = [
    "user_argocd",
    "user_cloud",
    "user_cloud_names",
    "user_cluster",
    "user_cluster_names",
    "user_connections",
    "user_kargo",
]
