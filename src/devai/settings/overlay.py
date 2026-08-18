"""PrincipalSettingsOverlay — per-user view of Settings over the global config.

This is the bridge that makes per-user/per-tenant connectors "dynamically
connect to the adapters". It wraps the global ``Settings`` object and overrides
just the attributes a Principal has configured (provider choices, models,
endpoints, and resolved secret values), with scope resolution:

    user → team(s) → tenant → global

Because the existing adapter factories read config via ``getattr(settings, ...)``,
handing them an overlay instead of the global ``Settings`` transparently routes
a user's own LLM/SCM/MCP credentials into the very same factories — no factory
changes, no duplication.

Secret values are resolved lazily from the secrets backend and cached on the
overlay for its (short) lifetime, so building one is cheap until a secret is
actually read.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from devai.settings.models import CONNECTOR_BY_KEY, Connector, Scope

if TYPE_CHECKING:
    from devai.identity import Principal
    from devai.settings.service import SettingsService

logger = logging.getLogger(__name__)


class PrincipalSettingsOverlay:
    """A read-only settings facade: per-Principal overrides over the base config.

    Attribute access falls through to the base ``Settings`` unless the Principal
    configured a connector field that maps to that attribute (via
    ``ConnectorField.settings_attr``), in which case the override wins.
    """

    __slots__ = ("_base", "_overrides", "_override_scopes", "_mcp_servers", "_principal_email")

    def __init__(
        self,
        base: Any,
        overrides: dict[str, Any],
        *,
        override_scopes: dict[str, Scope] | None = None,
        mcp_servers: list[dict[str, Any]] | None = None,
        principal_email: str = "",
    ) -> None:
        object.__setattr__(self, "_base", base)
        object.__setattr__(self, "_overrides", overrides)
        object.__setattr__(self, "_override_scopes", override_scopes or {})
        object.__setattr__(self, "_mcp_servers", mcp_servers or [])
        object.__setattr__(self, "_principal_email", principal_email)

    def __getattr__(self, name: str) -> Any:
        overrides = object.__getattribute__(self, "_overrides")
        if name in overrides:
            return overrides[name]
        return getattr(object.__getattribute__(self, "_base"), name)

    @property
    def mcp_servers(self) -> list[dict[str, Any]]:
        """Per-user MCP server connectors (name/url/token) for runner resolution."""
        servers: list[dict[str, Any]] = object.__getattribute__(self, "_mcp_servers")
        return servers

    @property
    def overlaid_attrs(self) -> list[str]:
        return sorted(object.__getattribute__(self, "_overrides").keys())

    @property
    def user_overlaid_attrs(self) -> list[str]:
        scopes = object.__getattribute__(self, "_override_scopes")
        return sorted(name for name, scope in scopes.items() if scope is Scope.USER)

    def __repr__(self) -> str:
        return (
            f"PrincipalSettingsOverlay(user={object.__getattribute__(self, '_principal_email')!r}, "
            f"overrides={self.overlaid_attrs})"
        )


async def build_overlay(
    base_settings: Any,
    principal: Principal | None,
    service: SettingsService | None,
) -> Any:
    """Resolve a Principal's connectors into a settings overlay.

    Returns the base settings unchanged when there's nothing to overlay (no
    service, no principal, or no configured connectors) — so callers can always
    use the return value as their settings object.
    """
    if service is None or principal is None:
        return base_settings

    # Build the scope lookup list in resolution order. Later wins are overwritten
    # by earlier (more specific) scopes, so we apply from least to most specific.
    lookups: list[tuple[Scope, str]] = [(Scope.GLOBAL, "")]
    if getattr(principal, "tenant_id", ""):
        lookups.append((Scope.TENANT, principal.tenant_id))
    # Org scope (teams.org_id) sits between tenant and team — a shared
    # connector for the whole org. Resolved ONLY for the principal's verified
    # org_ids (derived from team membership), so it never crosses orgs.
    for org_id in getattr(principal, "org_ids", []) or []:
        lookups.append((Scope.ORG, org_id))
    for team_id in getattr(principal, "team_ids", []) or []:
        lookups.append((Scope.TEAM, team_id))
    # Tenant principals use one tenant-qualified subject key. Never fall back
    # to an unqualified uid/email here: the same subject can exist in another
    # tenant. Tenantless/local principals retain the legacy uid/email lookup.
    uid = getattr(principal, "uid", "") or ""
    email = getattr(principal, "email", "") or ""
    tenant_id = getattr(principal, "tenant_id", "") or ""
    if tenant_id:
        user_scope_id = getattr(principal, "user_scope_id", "") or f"{tenant_id}:{uid or email}"
        if user_scope_id:
            lookups.append((Scope.USER, user_scope_id))
    else:
        if email and email != uid:
            lookups.append((Scope.USER, email))
        if uid:
            lookups.append((Scope.USER, uid))

    # Collect connectors per (key, instance) with most-specific-wins.
    merged: dict[tuple[str, str], Connector] = {}
    try:
        for scope, scope_id in lookups:
            for c in await service.list_connectors(scope, scope_id):
                if c.enabled:
                    merged[(c.connector_key, c.instance_id)] = c
        # Bridge uid/email: connectors are saved under the GIP uid, but runs
        # only carry the email. Match user connectors by email (scope_id OR
        # updated_by) so per-user LLM resolves at run time. Applied LAST so it
        # wins (it's the user's own, most-specific scope).
        if not tenant_id and email and hasattr(service, "list_user_connectors_by_email"):
            for c in await service.list_user_connectors_by_email(email):
                if c.enabled:
                    merged[(c.connector_key, c.instance_id)] = c
    except Exception:  # noqa: BLE001
        logger.warning("overlay: connector resolution failed — using base settings", exc_info=True)
        return base_settings

    if not merged:
        return base_settings

    overrides: dict[str, Any] = {}
    override_scopes: dict[str, Scope] = {}
    mcp_servers: list[dict[str, Any]] = []

    def set_override(name: str, value: Any, scope: Scope) -> None:
        overrides[name] = value
        override_scopes[name] = scope

    for (connector_key, _inst), c in merged.items():
        spec = CONNECTOR_BY_KEY.get(connector_key)
        if spec is None:
            continue

        # MCP is multi + special: collect into the mcp_servers list.
        if connector_key == "mcp":
            token = await _resolve(service, c, "mcp_token")
            mcp_servers.append(
                {
                    "name": c.prefs.get("mcp_name", c.instance_id),
                    "url": c.prefs.get("mcp_url", ""),
                    "token": token or "",
                    "transport": c.provider or "streamable_http",
                }
            )
            continue

        # Provider selection attribute.
        if c.provider and spec.provider_attr:
            set_override(spec.provider_attr, _coerce_provider(spec.provider_attr, c.provider), c.scope)

        # Non-secret prefs → their settings_attr.
        for fld in spec.fields:
            if fld.secret:
                continue
            if fld.key in c.prefs and c.prefs[fld.key] not in ("", None):
                set_override(fld.settings_attr, c.prefs[fld.key], c.scope)

        # Secret fields → resolve value from the backend.
        for fld in spec.fields:
            if not fld.secret:
                continue
            if fld.key in c.secret_refs:
                value = await _resolve(service, c, fld.key)
                if value:
                    set_override(fld.settings_attr, value, c.scope)

        # SCM auth method: inferred from WHICH credentials the user supplied,
        # so a user picks PAT *or* GitHub App without a separate selector. App
        # path needs all three (id + installation + key); otherwise a token is
        # the PAT/gitlab/ado path. This is what delinks a user from the
        # platform's global GitHub App — their connector drives create_scm_client.
        if connector_key == "scm":
            has_app = (
                bool(c.prefs.get("github_app_id"))
                and bool(c.prefs.get("github_app_installation_id"))
                and bool(c.secret_refs.get("github_app_private_key"))
            )
            if has_app:
                set_override("scm_auth_method", "github_app", c.scope)
            elif c.secret_refs.get("scm_token"):
                provider = (c.provider or "github").lower()
                set_override(
                    "scm_auth_method",
                    {
                        "github": "pat",
                        "gitlab": "gitlab_token",
                        "azure_devops": "ado_pat",
                    }.get(provider, "pat"),
                    c.scope,
                )

        # LLM model policy: the user's enabled-models choice (Settings UI
        # toggles). Stored as a list or comma-joined string in prefs;
        # exposed as `llm_enabled_models` for the resolver's allowlist wrap.
        if connector_key == "llm":
            raw_enabled = c.prefs.get("enabled_models")
            if isinstance(raw_enabled, str):
                enabled = [m.strip() for m in raw_enabled.split(",") if m.strip()]
            elif isinstance(raw_enabled, list):
                enabled = [str(m).strip() for m in raw_enabled if str(m).strip()]
            else:
                enabled = []
            if enabled:
                set_override("llm_enabled_models", enabled, c.scope)
            fb_model = c.prefs.get("fallback_model")
            if isinstance(fb_model, str) and fb_model.strip():
                set_override("llm_user_fallback_model", fb_model.strip(), c.scope)

    return PrincipalSettingsOverlay(
        base_settings,
        overrides,
        override_scopes=override_scopes,
        mcp_servers=mcp_servers,
        principal_email=getattr(principal, "email", ""),
    )


async def _resolve(service: SettingsService, c: Connector, field_key: str) -> str | None:
    ref = c.secret_refs.get(field_key)
    if not ref:
        return None
    return await service.resolve_secret(ref)


def _coerce_provider(attr: str, value: str) -> Any:
    """Some provider attrs are booleans (e.g. slack_enabled = on/off)."""
    if attr.endswith("_enabled"):
        return value.lower() in ("on", "true", "1", "yes")
    return value


__all__ = ["PrincipalSettingsOverlay", "build_overlay"]
