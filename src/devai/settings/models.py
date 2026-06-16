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
    """Ownership scope for a connector. Resolution (most→least specific):
    user → team → org → tenant → global. ``org`` is the business org
    (teams.org_id, verified via team membership); ``tenant`` is the auth pool.
    """

    USER = "user"
    TEAM = "team"
    ORG = "org"
    TENANT = "tenant"
    GLOBAL = "global"

    @classmethod
    def order(cls) -> list[Scope]:
        return [cls.USER, cls.TEAM, cls.ORG, cls.TENANT, cls.GLOBAL]


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
        providers=("anthropic", "openai", "vertex_gemini", "gateway", "groq", "openrouter", "noop"),
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
            _f(
                "vertex_project",
                "GCP Project",
                "vertex_project",
                placeholder="my-gcp-project",
                help="Project whose Vertex AI quota and billing this uses.",
                provider="vertex_gemini",
            ),
            _f(
                "vertex_location",
                "Location",
                "vertex_location",
                placeholder="global | asia-south1 | us-central1",
                provider="vertex_gemini",
            ),
            _f(
                "vertex_gemini_model",
                "Gemini Model",
                "vertex_gemini_model",
                placeholder="gemini-2.5-flash",
                provider="vertex_gemini",
            ),
            _f(
                "vertex_api_key",
                "Vertex API Key",
                "vertex_api_key",
                secret=True,
                placeholder="AQ....",
                help="Optional — leave empty on GKE to use keyless Workload Identity (ADC).",
                provider="vertex_gemini",
            ),
            _f(
                "vertex_base_url",
                "Gateway Base URL",
                "vertex_base_url",
                placeholder="(blank = direct to Vertex over PSC)",
                help="Route via an LLM gateway that injects credentials.",
                provider="vertex_gemini",
            ),
            _f(
                "llm_gateway_base_url",
                "Gateway Endpoint",
                "llm_gateway_base_url",
                required=True,
                placeholder="http://ai-gateway.agentgateway-system.svc.cluster.local:8080/v1",
                help="OpenAI-compatible endpoint; the gateway routes model aliases to any backend.",
                provider="gateway",
            ),
            _f(
                "llm_gateway_api_key",
                "Gateway Token",
                "llm_gateway_api_key",
                secret=True,
                provider="gateway",
            ),
            _f(
                "llm_gateway_model",
                "Default Model Alias",
                "llm_gateway_model",
                provider="gateway",
            ),
            _f(
                "groq_api_key",
                "Groq API Key",
                "groq_api_key",
                secret=True,
                placeholder="gsk_...",
                provider="groq",
            ),
            _f(
                "groq_model",
                "Groq Model",
                "groq_model",
                placeholder="llama-3.3-70b-versatile",
                provider="groq",
            ),
            _f(
                "openrouter_api_key",
                "OpenRouter API Key",
                "openrouter_api_key",
                secret=True,
                placeholder="sk-or-...",
                help="One key for hundreds of models (Llama, Mistral, DeepSeek, ...).",
                provider="openrouter",
            ),
            _f(
                "openrouter_model",
                "OpenRouter Model",
                "openrouter_model",
                placeholder="meta-llama/llama-3.3-70b-instruct",
                provider="openrouter",
            ),
        ),
    ),
    ConnectorSpec(
        key="scm",
        label="Source Control",
        family="scm",
        provider_attr="scm_provider",
        providers=("github", "gitlab", "azure_devops"),
        description=(
            "The git host DevAI reads from and opens PRs against — connected with YOUR own "
            "credentials, not the platform's. Use a Personal Access Token, or (GitHub only) "
            "your own GitHub App. The auth method is inferred from which fields you fill."
        ),
        fields=(
            _f(
                "scm_token",
                "Access Token / PAT",
                "scm_token",
                secret=True,
                placeholder="ghp_...",
                help="PAT path — GitHub/GitLab/ADO token. Leave blank if using a GitHub App below.",
            ),
            _f("scm_base_url", "API Base URL", "scm_base_url", placeholder="(blank for public github.com)"),
            _f("scm_organization", "Organization", "scm_organization"),
            # ── GitHub App path (GitHub only) — an alternative to the PAT. Fill
            # all three and DevAI authenticates as YOUR app (JWT → installation
            # token), used for both REST and GraphQL.
            _f(
                "github_app_id",
                "GitHub App ID",
                "github_app_id",
                placeholder="123456",
                help="GitHub App: numeric App ID. Fill App ID + Installation ID + Private Key to use App auth.",
                provider="github",
            ),
            _f(
                "github_app_installation_id",
                "GitHub App Installation ID",
                "github_app_installation_id",
                placeholder="12345678",
                help="GitHub App: the installation ID on your org/account.",
                provider="github",
            ),
            _f(
                "github_app_private_key",
                "GitHub App Private Key (PEM)",
                "github_app_private_key",
                secret=True,
                placeholder="-----BEGIN RSA PRIVATE KEY-----",
                help="GitHub App: the PEM private key. Stored in your own GCP Secret Manager scope.",
                provider="github",
            ),
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
        key="kagent",
        label="kagent Runtime",
        family="runtime",
        # provider_attr ends with `_enabled`, so the overlay coerces the
        # on/off provider value straight to the bool `kagent_enabled`.
        provider_attr="kagent_enabled",
        providers=("on", "off"),
        description=(
            "Run agents labelled `devai.io/runtime=kagent` as long-lived kagent "
            "Deployments reached over A2A, instead of one-shot Kubernetes Jobs. "
            "Off = always use Jobs. Leave the fields blank to use the platform's "
            "kagent controller; set them to point at your own."
        ),
        fields=(
            _f(
                "kagent_url",
                "Controller URL",
                "kagent_url",
                placeholder="http://kagent-controller.kagent-system.svc.cluster.local:8083",
                help="kagent controller A2A endpoint. Blank uses the platform default.",
            ),
            _f(
                "kagent_namespace",
                "Namespace",
                "kagent_default_namespace",
                placeholder="kagent-system",
                help="Namespace the controller serves agents under.",
            ),
        ),
    ),
    ConnectorSpec(
        key="mcp",
        label="MCP Server",
        family="mcp",
        provider_attr="mcp_endpoint",
        providers=("streamable_http", "sse"),
        multi=True,
        description=(
            "Connect external MCP servers your agents can call — add as many as you need. "
            "Tokens live in GCP Secret Manager under YOUR scope only; other tenants never "
            "see your servers or credentials."
        ),
        fields=(
            _f("mcp_name", "Name", "mcp_name", required=True, placeholder="my-tools"),
            _f("mcp_url", "Endpoint URL", "mcp_url", required=True, placeholder="https://host/mcp"),
            _f("mcp_token", "Auth Token", "mcp_token", secret=True),
            _f(
                "mcp_auth_header",
                "Auth Header",
                "mcp_auth_header",
                placeholder="Authorization (default) | x-api-key | ...",
                help="Header the token is sent under. Default sends 'Authorization: Bearer <token>'.",
            ),
        ),
    ),
    ConnectorSpec(
        key="kubernetes",
        label="Kubernetes Cluster",
        family="kubernetes",
        provider_attr="k8s_flavor",
        providers=("generic", "gke", "eks", "aks"),
        multi=True,
        description=(
            "Connect your own Kubernetes clusters — agents and GitOps tools (Argo CD, Kargo, "
            "Flux) can then operate against them by name. Tokens and CA certs live in GCP "
            "Secret Manager under YOUR scope only."
        ),
        fields=(
            _f("k8s_name", "Cluster Name", "k8s_name", required=True, placeholder="prod-gke"),
            _f(
                "k8s_api_server",
                "API Server URL",
                "k8s_api_server",
                required=True,
                placeholder="https://34.x.x.x or https://my-cluster:6443",
            ),
            _f(
                "k8s_token",
                "Bearer Token",
                "k8s_token",
                secret=True,
                required=True,
                help="ServiceAccount token with the RBAC your agents should have.",
            ),
            _f(
                "k8s_ca_cert",
                "CA Certificate (base64)",
                "k8s_ca_cert",
                secret=True,
                help="certificate-authority-data from your kubeconfig. Leave empty to skip TLS verification.",
            ),
            _f("k8s_namespace", "Default Namespace", "k8s_namespace", placeholder="default"),
        ),
    ),
    ConnectorSpec(
        key="cloud",
        label="Cloud Account",
        family="cloud",
        provider_attr="cloud_provider",
        providers=("gcp", "aws", "azure"),
        multi=True,
        description=(
            "Connect cloud accounts (GCP / AWS / Azure). Credentials are stored in GCP Secret "
            "Manager under your scope and surfaced to agents and MCP tools that operate on "
            "your infrastructure."
        ),
        fields=(
            _f("cloud_name", "Account Name", "cloud_name", required=True, placeholder="my-prod-gcp"),
            _f("gcp_project_id", "GCP Project ID", "gcp_project_id", provider="gcp"),
            _f(
                "gcp_sa_key",
                "Service Account Key (JSON)",
                "gcp_sa_key",
                secret=True,
                provider="gcp",
                help="Paste the JSON key. Prefer a least-privilege SA.",
            ),
            _f("aws_region", "AWS Region", "aws_region", placeholder="us-east-1", provider="aws"),
            _f("aws_access_key_id", "Access Key ID", "aws_access_key_id", secret=True, provider="aws"),
            _f("aws_secret_access_key", "Secret Access Key", "aws_secret_access_key", secret=True, provider="aws"),
            _f("azure_subscription_id", "Subscription ID", "azure_subscription_id", provider="azure"),
            _f("azure_tenant_id", "Tenant ID", "azure_tenant_id", provider="azure"),
            _f("azure_client_id", "Client ID", "azure_client_id", provider="azure"),
            _f("azure_client_secret", "Client Secret", "azure_client_secret", secret=True, provider="azure"),
        ),
    ),
    ConnectorSpec(
        key="argocd",
        label="Argo CD",
        family="gitops",
        provider_attr="argocd_mode",
        providers=("api", "kubectl"),
        multi=True,
        description=(
            "Connect external Argo CD instances by API server + token. The in-platform Argo CD "
            "needs nothing here; add entries for the Argo CDs running in YOUR clusters."
        ),
        fields=(
            _f("argocd_name", "Name", "argocd_name", required=True, placeholder="prod-argocd"),
            _f(
                "argocd_server_url",
                "Server URL",
                "argocd_server_url",
                required=True,
                placeholder="https://argocd.example.com",
            ),
            _f("argocd_token", "Auth Token", "argocd_token", secret=True, required=True),
            _f(
                "argocd_app_namespace",
                "Application Namespace",
                "argocd_app_namespace",
                placeholder="argocd",
                help="Namespace holding the Application CRs (kubectl mode).",
            ),
        ),
    ),
    ConnectorSpec(
        key="kargo",
        label="Kargo",
        family="gitops",
        provider_attr="kargo_mode",
        providers=("api", "kubectl"),
        multi=True,
        description=(
            "Connect external Kargo control planes (promotion pipelines in your own clusters) "
            "by API endpoint + token."
        ),
        fields=(
            _f("kargo_name", "Name", "kargo_name", required=True, placeholder="prod-kargo"),
            _f(
                "kargo_api_url",
                "API URL",
                "kargo_api_url",
                required=True,
                placeholder="https://kargo.example.com",
            ),
            _f("kargo_token", "Auth Token", "kargo_token", secret=True, required=True),
            _f("kargo_project", "Default Project", "kargo_project", placeholder="my-project"),
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
