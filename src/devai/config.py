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

    # --- PostgreSQL (persistent lifecycle store) ---
    database_url: str = "postgresql://devai:devai@localhost:5432/devai"

    # --- NATS ---
    nats_url: str = "nats://localhost:4222"
    nats_stream: str = "DEVAI"
    nats_max_deliver: int = 3
    nats_ack_wait: int = 300  # seconds

    # --- Event-bus adapter ---
    # Single switch picks the pub/sub backend; the rest of DevAI talks
    # only to `devai.adapters.event_bus.EventBusAdapter`. Swap providers
    # with one env var, no code changes. Missing SDK / unreachable broker
    # degrade gracefully to "noop" (in-process loopback) so the pod
    # never crashes on a transient broker outage.
    event_bus_provider: str = "nats"  # noop | nats

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

    # --- Repo onboarding (Repos page) ---
    # When true, the API runs a one-shot reconcile ~30s after boot so the
    # onboarding cache self-heals from the `.platform/devai.yaml` markers
    # (source of truth) after a DB wipe or a fresh deploy. Endpoint-driven
    # reconcile is always available regardless of this flag.
    onboarding_reconcile_on_boot: bool = True

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

    # --- Fiber-style blueprint runtime ---
    # When `pipeline_enabled` is True, webhooks and the SRE scanner dispatch
    # tasks through the new YAML-blueprint runtime (devai.pipeline.Pipeline)
    # instead of the legacy LangGraph orchestrators. The legacy path stays
    # wired so the cut-over is reversible by flipping a single env var.
    pipeline_enabled: bool = True
    pipeline_blueprint_dir: str = "blueprints"
    pipeline_default_blueprint: str = "alm-pipeline"
    pipeline_pr_review_blueprint: str = "pr-review"
    pipeline_sre_blueprint: str = "sre-monitor"
    pipeline_concurrency: int = 4
    pipeline_default_stage_timeout: int = 900  # 15 min — Fiber default
    pipeline_event_ring_size: int = 1000  # SSE replay buffer
    pipeline_task_ttl: int = 86400 * 30  # 30 days — keep parity with redis_result_ttl

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

    # --- K8s Job runtime (Spec-Driven Development runner) ---
    # When `k8s_runtime_enabled` is True, the blueprint executor spawns
    # one K8s Job per agent run instead of executing the agent in-process.
    # The Job pulls its skills/prompts/mcp-servers from the registry at
    # boot, runs the agent, writes a RESULT:: line to stdout, and exits.
    k8s_runtime_enabled: bool = False
    k8s_runtime_namespace: str = "devai"
    k8s_runner_service_account: str = "devai-runner"
    k8s_pull_secret_name: str = ""
    k8s_job_ttl_seconds: int = 3600
    k8s_job_backoff_limit: int = 0

    # Runner base image — entrypoint resolves agent + skills from registry.
    runner_image: str = "ghcr.io/tesserix/devai/devai-runner:main"

    # Per-stack runner images. The scaffold + preview stages pick a
    # stack-specific image (Next.js, Vite, Go, …) so dev-server frameworks
    # don't bloat the base runner.
    runner_image_per_stack: dict[str, str] = Field(
        default_factory=lambda: {
            "default": "ghcr.io/tesserix/devai/devai-runner:main",
            "nextjs": "ghcr.io/tesserix/devai/devai-runner-nextjs:main",
            "vite": "ghcr.io/tesserix/devai/devai-runner-vite:main",
            "go": "ghcr.io/tesserix/devai/devai-runner-go:main",
            "python": "ghcr.io/tesserix/devai/devai-runner-python:main",
        }
    )

    # Live preview pods route at `preview-<run_id>.<preview_domain>` and
    # the Claude-Code editor bridge at `editor-<run_id>.<preview_domain>`.
    preview_domain: str = "devai.tesserix.app"
    editor_bridge_image: str = "ghcr.io/tesserix/devai/devai-editor-bridge:main"

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

    # --- Specializations (Fiber-style YAML role catalog) ---
    specializations_enabled: bool = True
    specializations_dir: str = "specializations"

    # --- Agent Registry (aregistry HTTP client) ---
    # The shared catalog of skills + prompts + MCP servers + agents.
    # When `registry_url` is empty, DevAI runs in pure-local-YAML mode
    # (the seeds in architecture/registry-seeds/ are the source of
    # truth). When set, every loader prefers the registry and falls
    # back to local YAML only on miss / error. See
    # src/devai/registry/.
    registry_url: str = ""
    registry_token: str = ""
    registry_timeout_seconds: float = 5.0
    registry_cache_ttl_seconds: float = 30.0

    # --- registry adapter ---
    # Which registry backend the adapter family talks to. The registry itself
    # never depends on DevAI; swap backend with one env var.
    #   tesserix        → our open-source agentic-registry (/v0/* API)  [default]
    #   solo_aregistry  → upstream solo.io aregistry (/v0/* API)
    #   mcp_registry    → official MCP Registry / any /v0.1-compatible registry
    #   portkey         → Portkey prompt control plane
    #   noop            → empty catalogs (fall back to in-tree YAML seeds)
    # See src/devai/adapters/registry/.
    registry_provider: str = "tesserix"
    solo_registry_url: str = ""  # base URL when registry_provider=solo_aregistry
    mcp_registry_url: str = ""  # base URL when registry_provider=mcp_registry
    portkey_api_key: str = ""  # when registry_provider=portkey (ref only; vault-backed)

    # Secure registry connection (OAuth 2.1 client-credentials, preferred over
    # a static registry_token). DevAI authenticates as an OIDC client and gets a
    # short-lived scoped JWT the registry verifies via JWKS. The client secret
    # is sourced from GCP Secret Manager — never stored in the registry.
    registry_oidc_token_url: str = ""  # IdP token endpoint
    registry_client_id: str = ""
    registry_client_secret: str = ""  # from GCP Secret Manager / External Secret
    registry_scopes: str = "registry:read registry:write"
    registry_audience: str = ""

    # --- Agent control plane (agentgateway + kagent) ---
    # When set, the runner routes MCP traffic through agentgateway
    # (solo.io) instead of dialing each MCP server's Service directly.
    # That hands traffic policy + observability to the gateway. Empty
    # means "no gateway, talk direct" — useful in dev clusters that
    # haven't installed agentgateway yet.
    #
    # kagent_url is reserved for the future A2A handoff path; setting
    # it today is harmless (the runner only reads it when an agent
    # explicitly tries to hand off to another runner pod).
    agentgateway_url: str = ""
    kagent_url: str = ""

    # --- LLM adapter ---
    # Single switch picks the default LLM backend; specialization YAMLs
    # override per-role via their `llm_provider:` field. Missing SDKs /
    # config degrade gracefully to "noop" — adapter pattern, identical
    # to the memory family. See src/devai/adapters/llm/.
    llm_provider: str = "anthropic"  # noop | anthropic | openai
    llm_noop_canned_text: str = "[noop response]"

    # Optional per-provider overrides (the existing anthropic_api_key /
    # openai_api_key / openai_model / claude_model fields above feed the
    # factory).
    anthropic_base_url: str = ""
    openai_base_url: str = ""
    openai_organization: str = ""

    # --- Memory adapter (Agentic AI Memory) ---
    # Single switch picks the backend; the rest of DevAI talks only to
    # `devai.adapters.memory.MemoryAdapter`. Swap providers with one env var,
    # no code changes. Missing SDKs / config degrade gracefully to "noop".
    memory_provider: str = "redis"  # noop | redis | pgvector | mem0 | zep

    # mem0 (cloud or self-hosted). Cloud needs only DEVAI_MEM0_API_KEY;
    # self-hosted needs DEVAI_MEM0_HOST (and optionally an API key).
    mem0_api_key: str = ""
    mem0_host: str = ""

    # Zep (always self-hosted or Zep Cloud — both need a URL).
    zep_url: str = ""
    zep_api_key: str = ""

    # Hondo (cloud or self-hosted — at least one of url/api_key required).
    hondo_url: str = ""
    hondo_api_key: str = ""

    # Internal toggle — used by the NoopMemoryAdapter test mode.
    # Production should leave this False so a misconfigured provider can't
    # silently accumulate memories in process memory.
    memory_noop_keep_in_memory: bool = False

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
