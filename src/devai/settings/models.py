"""Connector catalog + data models for the Settings capability.

``CONNECTOR_SPECS`` is the single source of truth for what can be configured.
Each field declares the ``Settings`` attribute it maps to (``settings_attr``)
and whether it's a secret — that mapping is what lets the resolved overlay feed
straight into the existing ``getattr(settings, ...)`` adapter factories.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Scope(str, Enum):
    """Ownership scope for a connector. Resolution order: user→team→tenant→global."""

    USER = "user"
    TEAM = "team"
    TENANT = "tenant"
    GLOBAL = "global"

    @classmethod
    def order(cls) -> list[Scope]:
        return [cls.USER, cls.TEAM, cls.TENANT, cls.GLOBAL]


@dataclass(frozen=True, slots=True)
class ConnectorField:
    """One configurable field of a connector."""

    key: str  # logical field key, e.g. "anthropic_api_key"
    label: str
    settings_attr: str  # the Settings attribute this overrides in the overlay
    secret: bool = False  # secret → value stored in GCP SM, only a ref in PG
    required: bool = False
    placeholder: str = ""
    help: str = ""
    # For multi-provider connectors (e.g. observability), the provider this
    # field belongs to. Blank = shown for every provider. The Settings UI
    # shows only the fields whose provider matches the selected one.
    provider: str = ""


@dataclass(frozen=True, slots=True)
class ConnectorSpec:
    """A connector type: a family + its provider choices + fields."""

    key: str  # connector key, e.g. "llm", "scm", "mcp"
    label: str
    family: str  # adapter family / subsystem this drives
    provider_attr: str  # Settings attr that selects the provider (e.g. llm_provider)
    providers: tuple[str, ...]  # selectable provider values
    fields: tuple[ConnectorField, ...]
    description: str = ""
    multi: bool = False  # e.g. MCP: a user can add many of these


def _f(key: str, label: str, attr: str, **kw: Any) -> ConnectorField:
    return ConnectorField(key=key, label=label, settings_attr=attr, **kw)


# ── The catalog ────────────────────────────────────────────────────────────
CONNECTOR_SPECS: tuple[ConnectorSpec, ...] = (
    ConnectorSpec(
        key="llm",
        label="LLM Provider",
        family="llm",
        provider_attr="llm_provider",
        providers=("anthropic", "openai", "noop"),
        description="The model provider that powers your agents and chat.",
        fields=(
            _f(
                "anthropic_api_key",
                "Anthropic API Key",
                "anthropic_api_key",
                secret=True,
                placeholder="sk-ant-...",
                help="Used when provider = anthropic.",
                provider="anthropic",
            ),
            _f(
                "claude_model",
                "Claude Model",
                "claude_model",
                placeholder="claude-sonnet-4-20250514",
                provider="anthropic",
            ),
            _f(
                "openai_api_key",
                "OpenAI API Key",
                "openai_api_key",
                secret=True,
                placeholder="sk-...",
                provider="openai",
            ),
            _f("openai_model", "OpenAI Model", "openai_model", placeholder="gpt-4.1", provider="openai"),
        ),
    ),
    ConnectorSpec(
        key="scm",
        label="Source Control",
        family="scm",
        provider_attr="scm_provider",
        providers=("github", "gitlab", "azure_devops"),
        description="The git host DevAI reads from and opens PRs against.",
        fields=(
            _f(
                "scm_token",
                "Access Token / PAT",
                "scm_token",
                secret=True,
                placeholder="ghp_...",
                help="Personal access token or GitLab/ADO token.",
            ),
            _f("scm_base_url", "API Base URL", "scm_base_url", placeholder="(blank for public github.com)"),
            _f("scm_organization", "Organization", "scm_organization"),
        ),
    ),
    ConnectorSpec(
        key="memory",
        label="Agent Memory",
        family="memory",
        provider_attr="memory_provider",
        providers=("redis", "pgvector", "mem0", "zep", "hondo", "noop"),
        description="Where cross-run agent memory is stored.",
        fields=(
            _f("mem0_api_key", "mem0 API Key", "mem0_api_key", secret=True),
            _f("zep_api_key", "Zep API Key", "zep_api_key", secret=True),
            _f("zep_url", "Zep URL", "zep_url"),
        ),
    ),
    ConnectorSpec(
        key="slack",
        label="Slack",
        family="messaging",
        provider_attr="slack_enabled",
        providers=("on", "off"),
        description="Talk to DevAI from Slack (per-tenant workspace).",
        fields=(
            _f("slack_bot_token", "Bot Token", "slack_bot_token", secret=True, placeholder="xoxb-..."),
            _f("slack_signing_secret", "Signing Secret", "slack_signing_secret", secret=True),
        ),
    ),
    ConnectorSpec(
        key="mcp",
        label="MCP Server",
        family="mcp",
        provider_attr="mcp_endpoint",
        providers=("streamable_http", "sse"),
        multi=True,
        description="Connect an external MCP server your agents can call.",
        fields=(
            _f("mcp_name", "Name", "mcp_name", required=True, placeholder="my-tools"),
            _f("mcp_url", "Endpoint URL", "mcp_url", required=True, placeholder="https://host/mcp"),
            _f("mcp_token", "Bearer Token", "mcp_token", secret=True),
        ),
    ),
    ConnectorSpec(
        key="web_search",
        label="Web Search",
        family="web_search",
        provider_attr="web_search_provider",
        providers=("noop", "tavily"),
        description="Give agents a web-search tool.",
        fields=(_f("tavily_api_key", "Tavily API Key", "tavily_api_key", secret=True),),
    ),
    ConnectorSpec(
        key="observability",
        label="Observability",
        family="observability",
        provider_attr="observability_provider",
        providers=("prometheus", "datadog", "newrelic", "cloudwatch", "azure_monitor", "elasticsearch", "grafana"),
        multi=True,
        description=(
            "Connect monitoring backends the SRE runtime pulls metrics, logs, and alerts from. "
            "Add one or many — agents fan out across all connected sources."
        ),
        fields=(
            _f(
                "prometheus_url",
                "Prometheus URL",
                "prometheus_url",
                placeholder="http://prometheus:9090",
                provider="prometheus",
            ),
            _f("prometheus_token", "Prometheus Bearer Token", "prometheus_token", secret=True, provider="prometheus"),
            _f("datadog_api_key", "Datadog API Key", "datadog_api_key", secret=True, provider="datadog"),
            _f("datadog_app_key", "Datadog Application Key", "datadog_app_key", secret=True, provider="datadog"),
            _f("datadog_site", "Datadog Site", "datadog_site", placeholder="datadoghq.com", provider="datadog"),
            _f("newrelic_api_key", "New Relic User Key", "newrelic_api_key", secret=True, provider="newrelic"),
            _f("newrelic_account_id", "New Relic Account ID", "newrelic_account_id", provider="newrelic"),
            _f("cloudwatch_region", "AWS Region", "cloudwatch_region", placeholder="us-east-1", provider="cloudwatch"),
            _f("cloudwatch_log_group", "CloudWatch Log Group", "cloudwatch_log_group", provider="cloudwatch"),
            _f("azure_workspace_id", "Log Analytics Workspace ID", "azure_workspace_id", provider="azure_monitor"),
            _f("azure_resource_id", "Azure Resource ID (metrics)", "azure_resource_id", provider="azure_monitor"),
            _f("elasticsearch_url", "Elasticsearch URL", "elasticsearch_url", provider="elasticsearch"),
            _f(
                "elasticsearch_api_key",
                "Elasticsearch API Key",
                "elasticsearch_api_key",
                secret=True,
                provider="elasticsearch",
            ),
            _f("grafana_url", "Grafana URL", "grafana_url", provider="grafana"),
            _f("grafana_token", "Grafana Service-Account Token", "grafana_token", secret=True, provider="grafana"),
            _f("grafana_datasource_uid", "Grafana Datasource UID", "grafana_datasource_uid", provider="grafana"),
        ),
    ),
)

CONNECTOR_BY_KEY: dict[str, ConnectorSpec] = {c.key: c for c in CONNECTOR_SPECS}


@dataclass(slots=True)
class Connector:
    """A persisted connector instance (one row in the store).

    ``prefs`` holds non-secret field values. ``secret_refs`` maps a field key to
    the backend ``SecretRef`` name (the value lives in GCP SM, never here).
    """

    scope: Scope
    scope_id: str  # uid / team_id / tenant_id; "" for global
    connector_key: str  # e.g. "llm"
    provider: str = ""  # selected provider value
    instance_id: str = "default"  # for multi connectors (MCP), a stable id
    prefs: dict[str, Any] = field(default_factory=dict)
    secret_refs: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    updated_by: str = ""
    updated_at: str = ""

    def storage_key(self) -> str:
        return f"{self.scope.value}:{self.scope_id}:{self.connector_key}:{self.instance_id}"

    def public_dict(self) -> dict[str, Any]:
        """UI-safe view: prefs + which fields have a secret set (never values)."""
        return {
            "scope": self.scope.value,
            "scope_id": self.scope_id,
            "connector_key": self.connector_key,
            "provider": self.provider,
            "instance_id": self.instance_id,
            "prefs": dict(self.prefs),
            "secrets_set": sorted(self.secret_refs.keys()),
            "enabled": self.enabled,
            "updated_by": self.updated_by,
            "updated_at": self.updated_at,
        }


def catalog_public() -> list[dict[str, Any]]:
    """The connector catalog as JSON for the Settings UI."""
    out = []
    for c in CONNECTOR_SPECS:
        out.append(
            {
                "key": c.key,
                "label": c.label,
                "family": c.family,
                "provider_attr": c.provider_attr,
                "providers": list(c.providers),
                "description": c.description,
                "multi": c.multi,
                "fields": [
                    {
                        "key": f.key,
                        "label": f.label,
                        "secret": f.secret,
                        "required": f.required,
                        "placeholder": f.placeholder,
                        "help": f.help,
                        "provider": f.provider,
                    }
                    for f in c.fields
                ],
            }
        )
    return out


__all__ = [
    "CONNECTOR_BY_KEY",
    "CONNECTOR_SPECS",
    "Connector",
    "ConnectorField",
    "ConnectorSpec",
    "Scope",
    "catalog_public",
]
