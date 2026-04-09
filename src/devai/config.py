"""Application configuration via environment variables and settings files."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DEVAI_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # --- PostgreSQL (persistent lifecycle store) ---
    database_url: str = "postgresql://devai:devai@localhost:5432/devai"

    # --- NATS ---
    nats_url: str = "nats://localhost:4222"
    nats_stream: str = "DEVAI"
    nats_max_deliver: int = 3
    nats_ack_wait: int = 300  # seconds

    # --- Redis ---
    redis_url: str = "redis://localhost:6379"
    redis_result_ttl: int = 86400 * 30  # 30 days
    redis_lock_ttl: int = 360  # seconds

    # --- SCM Provider (GitHub / GitLab / Azure DevOps) ---
    scm_provider: str = "github"  # github | gitlab | azure_devops
    scm_auth_method: str = "github_app"  # github_app | pat | oauth | ado_pat | gitlab_token
    scm_base_url: str = ""  # Override default API URL (for self-hosted)
    scm_token: str = ""  # PAT / OAuth token / GitLab token
    scm_organization: str = ""  # ADO org name (if using azure_devops)

    # --- GitHub (legacy + GitHub App auth) ---
    github_app_id: int = 0
    github_app_private_key: str = ""
    github_app_installation_id: int = 0
    github_webhook_secret: str = ""
    github_org: str = "tesserix"

    # --- GitHub OAuth (for dashboard) ---
    github_oauth_client_id: str = ""
    github_oauth_client_secret: str = ""

    # --- Dashboard ---
    dashboard_base_url: str = "http://localhost:8080"

    # --- Keycloak OIDC (primary auth for dashboard) ---
    keycloak_url: str = "https://internal-identity.tesserix.app"
    keycloak_realm: str = "tesserix-internal"
    keycloak_client_id: str = "devai-dashboard"
    keycloak_client_secret: str = ""
    auth_provider: str = "keycloak"  # "keycloak" or "github"

    # --- OpenAI / Codex ---
    openai_api_key: str = ""
    openai_model: str = "gpt-4.1"

    # --- Anthropic / Claude ---
    anthropic_api_key: str = ""
    claude_model: str = "claude-sonnet-4-20250514"
    claude_max_tokens: int = 8192
    # Default ceiling for any agent loop. Heavy code-generation roles
    # (Senior Developer, DB Engineer, Infra Provisioner, QA Tester) pass an
    # explicit higher value via the per-role overrides below. The loop now
    # degrades gracefully when the ceiling is reached — it asks the model
    # to wrap up rather than raising — so this is a soft target, not a
    # hard kill switch.
    claude_max_iterations: int = 50
    # Per-role caps. These are passed explicitly by each agent's call site
    # so a config tweak doesn't require touching every agent file.
    claude_max_iterations_implementation: int = 120  # senior_developer, db_engineer
    claude_max_iterations_review: int = 80  # staff_reviewer, security_expert
    claude_max_iterations_ops: int = 100  # infra_provisioner, qa_tester
    claude_max_iterations_planning: int = 60  # engineering_manager

    # --- Google Gemini ---
    gemini_api_key: str = ""
    # Switched from the deprecated gemini-2.5-flash-preview-04-17 model
    # which now returns 404 from the v1beta API. gemini-3.1-pro-preview
    # is the most capable current preview; override at runtime via the
    # DEVAI_GEMINI_MODEL env var if you want a stable Flash variant for
    # cheaper / faster TechDetector calls.
    gemini_model: str = "gemini-3.1-pro-preview"
    gcp_secret_gemini_api_key: str = "prod-devai-gemini-api-key"

    # --- Groq (fallback / secondary) ---
    groq_api_key: str = ""
    groq_model: str = "llama-3.3-70b-versatile"
    gcp_secret_groq_api_key: str = "prod-devai-groq-api-key"

    # --- NemoClaw / Nemotron (self-hosted GPU inference) ---
    nemoclaw_api_key: str = ""  # "not-needed" for local vLLM/NIM
    nemoclaw_endpoint: str = ""  # e.g. http://nemoclaw-inference.devai.svc.cluster.local:8000/v1
    nemoclaw_model: str = "nvidia/nemotron-3-super-120b-a12b"
    nemoclaw_max_tokens: int = 8192
    nemoclaw_max_iterations: int = 50
    nemoclaw_fallback_to_groq: bool = True  # Fall back to Groq if GPU unavailable

    # --- Server ---
    host: str = "0.0.0.0"
    port: int = 8080
    log_level: str = "info"

    # --- Pipeline ---
    max_review_iterations: int = 3
    pipeline_label: str = "devai:automate"
    project_ready_column: str = "Ready for DevAI"

    # --- LangSmith ---
    langchain_tracing_v2: str = ""  # Set to "true" to enable
    langchain_api_key: str = ""  # LangSmith API key (lsv2_pt_xxx)
    langchain_project: str = "devai"  # Project name in LangSmith UI
    langchain_endpoint: str = "https://api.smith.langchain.com"

    # --- GKE / Kubernetes ---
    gke_cluster: str = "tesseract-prod-in-gke"
    gke_region: str = "asia-south1"
    gke_project: str = "tesseracthub-480811"
    gke_use_in_cluster: bool = True  # Use in-cluster auth when running in GKE

    # --- Monitoring ---
    prometheus_url: str = "http://prometheus-server.monitoring.svc.cluster.local:80"
    grafana_url: str = "http://grafana.monitoring.svc.cluster.local:80"

    # --- ArgoCD ---
    argocd_namespace: str = "argocd"
    argocd_sync_timeout: int = 300  # seconds to wait for ArgoCD sync
    argocd_health_timeout: int = 120  # seconds to wait for healthy status

    # --- Cloudflare ---
    cloudflare_api_token: str = ""
    cloudflare_account_id: str = ""
    cloudflare_zone_id: str = ""  # Primary zone for DNS/tunnel management

    # --- Observability ---
    otel_endpoint: str = ""
    metrics_enabled: bool = True

    @property
    def is_github_app_configured(self) -> bool:
        return self.github_app_id > 0 and len(self.github_app_private_key) > 0

    def export_langsmith_env(self) -> None:
        """Export LangSmith config as environment variables (required by LangChain)."""
        import os

        if self.langchain_api_key:
            os.environ["LANGCHAIN_API_KEY"] = self.langchain_api_key
            os.environ["LANGCHAIN_PROJECT"] = self.langchain_project
            os.environ["LANGCHAIN_ENDPOINT"] = self.langchain_endpoint
            if self.langchain_tracing_v2:
                os.environ["LANGCHAIN_TRACING_V2"] = self.langchain_tracing_v2


settings = Settings()
