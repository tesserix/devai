"""Application configuration via environment variables and settings files."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DEVAI_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # --- NATS ---
    nats_url: str = "nats://localhost:4222"
    nats_stream: str = "DEVAI"
    nats_max_deliver: int = 3
    nats_ack_wait: int = 300  # seconds

    # --- Redis ---
    redis_url: str = "redis://localhost:6379"
    redis_result_ttl: int = 86400 * 30  # 30 days
    redis_lock_ttl: int = 360  # seconds

    # --- GitHub ---
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
    openai_model: str = "o3"

    # --- Anthropic / Claude ---
    anthropic_api_key: str = ""
    claude_model: str = "claude-sonnet-4-20250514"
    claude_max_tokens: int = 8192
    claude_max_iterations: int = 25

    # --- Server ---
    host: str = "0.0.0.0"
    port: int = 8080
    log_level: str = "info"

    # --- Pipeline ---
    max_review_iterations: int = 3
    pipeline_label: str = "devai:automate"
    project_ready_column: str = "Ready for DevAI"

    # --- Observability ---
    otel_endpoint: str = ""
    metrics_enabled: bool = True

    @property
    def is_github_app_configured(self) -> bool:
        return self.github_app_id > 0 and len(self.github_app_private_key) > 0


settings = Settings()
