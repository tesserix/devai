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

    # --- Pipeline autonomy + resilience ---
    # Gate stages (gate: true) under "full" autonomy self-approve with an
    # audit trail so runs flow end-to-end without prompting; "gated" pauses
    # at every gate for a human decision. Per-run override via the trigger
    # request's `autonomy` field.
    pipeline_default_autonomy: str = "auto"  # auto | full | gated
    # auto  — smart: the plan-approval stage pauses only when the intent is
    #         judged ambiguous; static gates (staff-review/deploy) self-approve.
    # full  — never pause anywhere.
    # gated — pause at every gate.
    # Transient-failure retries per stage (exceptions/timeouts) before
    # on_failure semantics apply. Per-stage `retries:` in the blueprint
    # overrides. Stages are resume-idempotent, so retries are safe.
    pipeline_stage_retries: int = 1
    # Autonomous failure recovery: when a stage exhausts its retries, a
    # recovery agent reviews the failure (LLM root-cause), injects corrective
    # guidance, and re-runs the stage — pausing for human approval only when
    # the fix genuinely needs a decision. Runs BEFORE on_failure semantics.
    pipeline_heal_on_failure: bool = True
    # Recovery rounds per stage: each round files/updates a GitHub bug issue
    # with the diagnosis, injects corrective guidance, and re-runs the stage.
    # Exhausting all rounds posts a RUNBOOK (everything tried + what a human
    # should check) on the bug issue. Clamped to 5 by the executor.
    pipeline_heal_attempts: int = 3
    # Progress-aware stage liveness: a stage past its timeout is NOT killed
    # while its agent shows recent tool activity (devai:run:<id>:activity,
    # stamped by the tool layer) — it gets extended up to timeout × the hard
    # cap multiplier. Stalled stages (no activity within the grace window)
    # still die at the original timeout. "Never time out while working,
    # die fast when stuck."
    pipeline_stage_inactivity_grace: int = 240  # seconds of silence = stalled
    pipeline_stage_hard_cap_multiplier: int = 4  # absolute ceiling = timeout × N
    # How long a HARD approval gate (gate: true) waits for the human before
    # timing out RESUMABLY — the run lands in stage_failed with a clear
    # message and the Continue button re-requests approval at the same gate.
    pipeline_gate_timeout_seconds: int = 1800  # 30 minutes

    # --- NATS ---
    nats_url: str = "nats://localhost:4222"
    nats_stream: str = "DEVAI"
    nats_max_deliver: int = 3
    nats_ack_wait: int = 300  # seconds
    # Stream retention: False → LIMITS (observability fan-out: any number of
    # consumers, messages age out). True → WORK_QUEUE (exactly-once job
    # semantics). Retention can't change in place on an existing stream —
    # the adapter logs the migration step instead of recreating.
    nats_stream_work_queue: bool = False

    # --- Event-bus adapter ---
    # Single switch picks the pub/sub backend; the rest of DevAI talks
    # only to `devai.adapters.event_bus.EventBusAdapter`. Swap providers
    # with one env var, no code changes. Missing SDK / unreachable broker
    # degrade gracefully to "noop" (in-process loopback) so the pod
    # never crashes on a transient broker outage.
    event_bus_provider: str = "nats"  # noop | nats

    # --- Messaging / remote conversational channels ---
    # DevAI can be talked to from outside the dashboard: Slack, a remote
    # URL/thread, or any MCP client. All three are thin transports over one
    # ConversationGateway (devai.chat.gateway) — no duplicated chat logic.
    # Each channel is independently togglable; a disabled channel is simply
    # not mounted (degrades to the Noop messaging channel). Secrets come from
    # GCP Secret Manager in prod, never the DB or git.
    remote_chat_enabled: bool = True  # POST /threads/{id}/messages + SSE stream
    mcp_server_enabled: bool = True  # mount DevAI as an MCP server at /mcp
    slack_enabled: bool = False  # POST /webhook/slack (Events API)
    # Shared static bearer token guarding the remote URL + MCP endpoints.
    # Empty disables token auth (local/dev only); set in prod via Secret Manager.
    remote_chat_api_token: str = ""
    # When true, the turn runs on a NATS worker (ack-fast, reply-later) instead
    # of inline — required for Slack's 3s ack budget; also smooths slow turns.
    messaging_use_worker: bool = True
    messaging_turn_subject: str = "devai.chat.turn"  # NATS subject for handoff
    # Slack credentials (GCP Secret Manager in prod).
    slack_bot_token: str = ""  # xoxb-...
    slack_signing_secret: str = ""  # request-signature verification
    slack_allowed_channels: str = ""  # csv of channel IDs; empty = all

    # --- MCP Hub (devai-mcp-hub) ---
    # The Agentic MCP: one /mcp surface that multiplexes every registry-declared
    # downstream MCP (docs/agentic/MCP-HUB.md). Discovery is registry-driven, so
    # there is nothing to hardcode here — only operational knobs.
    mcp_hub_enabled: bool = True
    # Mount per-domain downstream MCP servers (/mcp/scm, /mcp/sample) on devai-api
    # so the Hub federates REAL runnable tools. Off by default until verified;
    # the devai-api chart enables it in-cluster. Only genuinely standalone tools
    # are served (SCM ops, sample) — pipeline-stage tools are not (MCP-HUB.md §8).
    mcp_downstream_servers_enabled: bool = False
    # Port the Hub's ASGI app listens on (its own Deployment, not devai-api).
    mcp_hub_port: int = 8095
    # Service token the Hub presents DOWNSTREAM for authMode=jwt servers (the
    # caller's identity is terminated at the Hub; callers never hold this).
    # Empty → no bearer injected (fine for authMode=none/header downstreams).
    mcp_hub_service_token: str = ""
    # How often the Hub re-discovers the registry + reconnects legs (seconds).
    mcp_hub_refresh_seconds: float = 60.0
    # Per-downstream connect timeout (seconds).
    mcp_hub_connect_timeout: float = 15.0

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
    # When true, the API runs a reconcile ~30s after boot so the onboarding
    # cache self-heals from the `.platform/devai.yaml` markers (the source of
    # truth) after a DB wipe or a fresh deploy. Endpoint-driven reconcile is
    # always available regardless of this flag.
    onboarding_reconcile_on_boot: bool = True
    # Periodic reconcile interval (seconds). After the boot reconcile, the
    # poller re-runs a batched GraphQL marker probe every N seconds so repos
    # that gained a marker show ONBOARDED and repos that lost one are offloaded
    # to DORMANT automatically — no manual trigger. Set 0 to disable the loop
    # (boot reconcile still runs once when onboarding_reconcile_on_boot=True).
    onboarding_reconcile_interval_seconds: int = 300

    # --- A2A (Agent2Agent) consumer security ---
    # When true (default), before the orchestrator calls a peer agent it
    # verifies the agent card's registry Ed25519 signature (over the artifact
    # digest) AND checks the card's service URL against the host allowlist —
    # defending against a tampered card that redirects agent traffic. Set false
    # ONLY for trusted local/anonymous dev where the registry isn't signing.
    a2a_secure: bool = True
    # Host suffixes a verified card's service URL may target. Defaults to
    # in-cluster service DNS; add public A2A agent hosts explicitly. A single
    # "*" entry disables the allowlist (still blocks private/link-local IPs).
    a2a_allowed_url_suffixes: list[str] = Field(default_factory=lambda: [".svc.cluster.local", ".svc"])

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
    # "keycloak" | "github" | "local_db". local_db is a LOCAL/kind-only
    # username+password mode for team logins (no GIP/Keycloak in the sandbox).
    # NEVER set local_db in prod — prod auth is terminated by the auth-bff.
    auth_provider: str = "keycloak"

    # --- Local DB auth (DEVAI_AUTH_PROVIDER=local_db, LOCAL ONLY) ---
    # JSON list of team logins, e.g. [{"username":"admin","password":"…",
    # "email":"admin@devai.local","name":"Admin","roles":["admin"]}, …].
    # Empty by default so no local users exist in prod.
    local_auth_users: list[dict] = Field(default_factory=list)

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
    # Conversational sessions: long agent loops run as a CHAIN of bounded
    # conversations instead of one ever-growing thread. Each session does at
    # most `claude_session_iterations` tool turns, checkpoints its progress
    # ("done / remaining"), and the next session starts FRESH from the brief
    # + that note. Keeps context small (rate limits, cost, latency) and makes
    # progress durable — a killed session loses minutes, not the whole build.
    claude_session_iterations: int = 15
    claude_max_sessions: int = 8
    # Prompt caching: cache_control breakpoints on system prompt, tool
    # schemas, and the newest user turn — every turn re-reads the prefix
    # from cache instead of re-paying full input tokens.
    claude_prompt_caching: bool = True
    # Absolute backstop for one agent loop (sessions bound the real work;
    # stage-level liveness supervision governs the practical budget).
    claude_agent_total_timeout: int = 14400
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

    # --- Security ---
    # When True, mutating requests (POST/PUT/PATCH/DELETE) must carry a
    # resolvable principal (auth-bff X-Forwarded-* headers or a valid
    # devai_session cookie) or they're rejected with 401. Default False
    # keeps today's "trust the auth-bff + Istio edge" behavior so flipping
    # this on is an opt-in defense-in-depth step, not a breaking change.
    # Webhook routes are always exempt — they authenticate via HMAC.
    require_auth: bool = False

    # Comma-separated emails granted the "admin" role on every authenticated
    # request. Prod principals arrive from the auth-bff with NO roles, so
    # without this nobody is admin — analytics then scopes everyone to their
    # personal usage view and the global/by-user rollups are unreachable.
    admin_emails: str = ""

    # Shared secret the auth-bff includes (header X-Auth-Bff-Secret) so the
    # backend only trusts X-Forwarded-* identity headers from the bff. When
    # empty, X-Forwarded-* is trusted unconditionally (today's behavior).
    # When set, forwarded identity is ignored unless the secret matches —
    # closing header-spoofing if the pod is ever reachable without the edge.
    auth_bff_shared_secret: str = ""

    # Whether X-Forwarded-* identity is trusted when no shared secret is set.
    # Default True preserves today's behavior: with auth_bff_shared_secret
    # empty, forwarded headers are trusted unconditionally (the auth-bff +
    # Istio edge is assumed). Set False to FAIL CLOSED — forwarded identity
    # is never trusted (and inbound X-Forwarded-* are stripped) unless a
    # non-empty secret is configured AND matches. Flip this on in prod once
    # the auth-bff stamps X-Auth-Bff-Secret (see CODE-5) so a pod reachable
    # without the edge can't be driven by spoofed identity headers.
    trust_forwarded_without_secret: bool = True

    # Shared AES-GCM key (16/24/32 bytes) the auth-bff uses to encrypt the
    # devai_session cookie (services/auth-bff/internal/session/cookie.go). The
    # dashboard proxies /api straight to devai-api, bypassing the bff, so
    # devai-api decrypts this cookie itself to authenticate the caller. Same
    # value as the bff's DEVAI_BFF_SESSION_ENCRYPT_KEY. Empty disables it.
    bff_session_encrypt_key: str = ""

    # When set, the document-ingestion agent tools (doc_read_pdf /
    # doc_read_markdown / doc_parse_openapi) may only read files resolving
    # inside this directory. Empty = no confinement (today's behavior).
    # Set to the per-run workspace root to stop a prompt-injected agent
    # from reading arbitrary files (e.g. /etc/passwd).
    tool_workspace_root: str = ""

    # --- Hardening flags (security review) ---
    # Each is also read at its use-site via getattr with the SAME default, so
    # behavior is identical whether or not these are declared — they live here
    # for discoverability. Defaults preserve today's behavior; flip the gates
    # on in prod values once the paired code/charts changes are deployed.

    # Idle-TTL (seconds) before an unused preview environment is reaped (CODE-11).
    # Env: DEVAI_PREVIEW_TTL_SECONDS. Default 4h.
    preview_ttl_seconds: int = 14400

    # MCP Hub: require an authenticated principal on /mcp (else 401) and apply
    # the SSRF allowlist when dialing registry-supplied downstream endpoints
    # (CODE-6). Default False keeps today's open behavior; set True in prod.
    mcp_hub_require_auth: bool = False
    mcp_hub_ssrf_enforce: bool = False

    # Authoring guardrails (CODE-21). Trusted registries an authored MCP-server
    # image may reference; per-principal / per-kind creation caps + rate limit
    # (0 = unlimited, today's behavior).
    authoring_trusted_image_registries: list[str] = Field(default_factory=lambda: ["ghcr.io/tesserix/"])
    authoring_per_kind_cap: int = 0
    authoring_per_principal_quota: int = 0
    authoring_rate_limit_per_minute: int = 0

    # GitHub OAuth: in addition to github_org membership, only these emails/
    # logins may mint a dashboard session (CODE-15). Empty = no allowlist.
    github_oauth_email_allowlist: list[str] = Field(default_factory=list)

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
    # Durable work queue. When True (default) the pipeline's queue lives in
    # Redis (reliable-queue pattern) so runs survive pod rolls / scale events
    # and any replica can pick them up — no more orphaned PENDING runs. Set
    # False to fall back to the legacy per-pod in-memory asyncio.Queue.
    pipeline_durable_queue: bool = True
    pipeline_reaper_interval: int = 30  # seconds between stale-claim sweeps
    pipeline_claim_ttl: int = 180  # seconds; refreshed every claim_ttl/4 while running
    pipeline_queue_poll_interval: float = 1.0  # seconds workers wait when the queue is empty
    pipeline_event_ring_size: int = 1000  # SSE replay buffer
    pipeline_task_ttl: int = 86400 * 30  # 30 days — keep parity with redis_result_ttl

    # --- workflow adapter (durable orchestration backbone) ---
    # Selects HOW blueprints execute. `inproc` (default) runs them in this
    # process via BlueprintExecutor — identical to the pre-adapter behaviour.
    # `temporal` runs the one generic BlueprintWorkflow on a Temporal cluster
    # (crash-safe resume, declarative retries/timeouts, durable approvals).
    # `noop` disables execution. The choice is invisible to blueprints and
    # agents — any blueprint runs on any backend with no code change.
    workflow_provider: str = "inproc"  # inproc | temporal | noop
    temporal_host: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "devai"
    temporal_tls_enabled: bool = False
    temporal_max_concurrent_activities: int = 50
    temporal_max_stage_attempts: int = 3  # per-stage Activity RetryPolicy

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
    # MUST be the asia-south1 GAR mirror, never ghcr.io: the runner Job pods
    # have no ghcr pull secret and pull via the node's GCP SA, which can pull
    # GAR (same project) but not ghcr.io — a ghcr image silently ImagePullBackOffs
    # and the stage times out. (Prod overrides this via DEVAI_RUNNER_IMAGE.)
    runner_image: str = "asia-south1-docker.pkg.dev/tesseracthub-480811/ghcr-remote/tesserix/devai/devai-runner:main"

    # Per-stack runner images. The scaffold + preview stages pick a
    # stack-specific image (Next.js, Vite, Go, …) so dev-server frameworks
    # don't bloat the base runner. All GAR-mirrored (see runner_image note).
    runner_image_per_stack: dict[str, str] = Field(
        default_factory=lambda: {
            "default": "asia-south1-docker.pkg.dev/tesseracthub-480811/ghcr-remote/tesserix/devai/devai-runner:main",
            "nextjs": "asia-south1-docker.pkg.dev/tesseracthub-480811/ghcr-remote/tesserix/devai/devai-runner-nextjs:main",
            "vite": "asia-south1-docker.pkg.dev/tesseracthub-480811/ghcr-remote/tesserix/devai/devai-runner-vite:main",
            "go": "asia-south1-docker.pkg.dev/tesseracthub-480811/ghcr-remote/tesserix/devai/devai-runner-go:main",
            "python": "asia-south1-docker.pkg.dev/tesseracthub-480811/ghcr-remote/tesserix/devai/devai-runner-python:main",
        }
    )

    # Live preview pods route at `preview-<run_id>.<preview_domain>` (a
    # single-level subdomain so the *.tesserix.app wildcard cert covers it),
    # behind the devai-auth-bff session gate. Per-session Services live in
    # `preview_namespace`.
    preview_domain: str = "tesserix.app"
    preview_namespace: str = "devai-previews"
    editor_bridge_image: str = (
        "asia-south1-docker.pkg.dev/tesseracthub-480811/ghcr-remote/tesserix/devai/devai-editor-bridge:main"
    )
    # The editor-bridge sidecar (in-browser Claude Code) is OFF until its image
    # is built + published — the live preview (dev server on a public URL) works
    # without it. Flip to True once devai-editor-bridge ships.
    preview_editor_bridge_enabled: bool = False

    # --- Monitoring ---
    prometheus_url: str = "http://prometheus-server.monitoring.svc.cluster.local:80"
    grafana_url: str = "http://grafana.monitoring.svc.cluster.local:80"
    # Browser-facing Grafana base (for analytics "open in Grafana" trace deep
    # links). Empty → the dashboard hides the link. The Tempo datasource uid
    # is the one registered in grafana.yaml (uid: tempo).
    grafana_public_url: str = ""
    traces_backend: str = "tempo"
    traces_datasource_uid: str = "tempo"

    # --- Observability connectors (configured in DevAI Settings; the SRE
    # runtime reads these from env via its ExternalSecret). The fan-out tool
    # queries whichever providers have creds present. ---
    observability_provider: str = ""  # informational; multi-provider via fan-out
    prometheus_token: str = ""
    datadog_api_key: str = ""
    datadog_app_key: str = ""
    datadog_site: str = "datadoghq.com"
    newrelic_api_key: str = ""
    newrelic_account_id: str = ""
    cloudwatch_region: str = "us-east-1"
    cloudwatch_log_group: str = ""
    azure_workspace_id: str = ""
    azure_resource_id: str = ""
    elasticsearch_url: str = ""
    elasticsearch_api_key: str = ""
    elasticsearch_username: str = ""
    elasticsearch_password: str = ""
    grafana_token: str = ""
    grafana_datasource_uid: str = ""

    # --- ArgoCD ---
    argocd_namespace: str = "argocd"
    argocd_sync_timeout: int = 300  # seconds to wait for ArgoCD sync
    argocd_health_timeout: int = 120  # seconds to wait for healthy status

    # --- gitops adapter ---
    # Backend for deps.gitops / the default GitOps tool target. One of:
    # argocd | kargo | flux | noop. See src/devai/adapters/gitops/.
    gitops_provider: str = "argocd"
    # Which backends the /mcp/gitops domain exposes side by side (comma list).
    # Each builds independently; one missing controller drops only its tools.
    gitops_mcp_providers: str = "argocd,kargo,flux"
    # Master gate for mutating GitOps operations (sync / promote / rollback /
    # reconcile / suspend). When false every backend refuses with a clear
    # message — read-only inspection keeps working.
    gitops_mutations_enabled: bool = True
    # Kargo project (namespace) assumed when a tool call omits one.
    kargo_default_project: str = ""
    # Namespace Flux objects default to when a tool call omits one.
    flux_namespace: str = "flux-system"
    # TLS policy for user-connected clusters: when true, a connected cluster
    # MUST carry a CA cert — gitops tools refuse to target a cluster with no
    # CA rather than falling back to --insecure-skip-tls-verify. Default false
    # keeps onboarding frictionless (the connector UI warns either way).
    gitops_require_cluster_ca: bool = False

    # --- cloud adapter ---
    # Backend for the cloud-account family (consumes Settings → Cloud Account
    # connectors). gcp | aws | azure | noop. Per-user creds resolve from the
    # caller's connector; this is only the platform default / fallback.
    cloud_provider: str = "noop"

    # --- Cloudflare ---
    cloudflare_api_token: str = ""
    cloudflare_account_id: str = ""
    cloudflare_zone_id: str = ""  # Primary zone for DNS/tunnel management

    # --- Observability / telemetry adapter (adapters/telemetry) ---
    # DEVAI_TELEMETRY_PROVIDER picks the backend: "noop" (default; discards) or
    # "otel" (OTLP/HTTP exporter → otel-collector). With metrics_enabled=False
    # the factory forces Noop regardless of provider. The OTel backend exports
    # traces + metrics for HTTP requests, pipeline stages, and LLM calls. The
    # endpoint is the collector's OTLP/HTTP base (port 4318); the adapter
    # appends /v1/traces and /v1/metrics.
    telemetry_provider: str = "noop"
    otel_endpoint: str = ""  # e.g. http://otel-collector.observability.svc.cluster.local:4318
    otel_service_name: str = "devai"
    otel_service_namespace: str = "devai"
    otel_export_interval_ms: int = 15000
    metrics_enabled: bool = True

    # --- Logging & SLOs (dashboard /logs page) ---
    # In-process log ring size (live "what is the service saying" view).
    log_buffer_capacity: int = 2000
    # GCS bucket holding archived pod logs (tesserix-k8s devai-log-archiver
    # CronJob writes; we only LIST it for the archive panel). Empty disables
    # the live listing — the panel still shows the configured policy.
    log_archive_bucket: str = ""
    # Days after which the purge CronJob deletes archived logs (mirrors the
    # bucket lifecycle's delete rule; surfaced read-only in the UI).
    log_purge_days: int = 15
    # SLO targets the /api/analytics/slo endpoint scores against.
    slo_availability_target_pct: float = 99.9
    slo_latency_p95_ms: float = 1500.0
    slo_error_rate_target_pct: float = 1.0

    # --- Specializations (Fiber-style YAML role catalog) ---
    specializations_enabled: bool = True
    specializations_dir: str = "specializations"

    # --- Crews (AI agent teams; seed catalog) ---
    crews_dir: str = "crews"

    # --- Teams (human teams that own crews) ---
    teams_enabled: bool = True

    # --- Web search adapter (grounded search + fetch for agents) ---
    # provider: noop | tavily. Key lives in GCP Secret Manager in prod.
    web_search_provider: str = "noop"
    web_search_api_key: str = ""
    web_search_max_results: int = 5

    # --- Object store adapter (composer image/attachment blobs) ---
    # provider: noop | gcs. Uses Workload Identity (ADC) — no key in config.
    object_store_provider: str = "noop"
    object_store_bucket: str = ""
    object_store_prefix: str = "devai"

    # --- Terminal sandbox (runner pod git worktree + line-protocol) ---
    terminal_sandbox_enabled: bool = False
    terminal_sandbox_timeout_seconds: int = 3600

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

    # --- Publish-on-author (devai-authored artifacts -> shared registry) ---
    # When an operator composes an agent/skill/tool/blueprint in the dashboard,
    # publish it to the shared registry so it's discoverable, A2A-carded and
    # signed alongside the rest of the catalog. Best-effort: a publish failure
    # never breaks the local hot-register (the author still gets a runnable
    # agent). Default off so a registry-less dev cluster behaves exactly as
    # before; tesserix-k8s flips this on in prod.
    registry_publish_enabled: bool = False
    # Registry tenant (namespace) authored artifacts are published under and
    # read back from — the uniqueness + isolation boundary in the catalog.
    # Deployment-configurable via DEVAI_REGISTRY_DEFAULT_TENANT (the devai-api
    # chart sets it from agenticControlPlane.registryDefaultTenant); NOT a code
    # constant. This is the ORG/registry tenant and is intentionally distinct
    # from Principal.tenant_id, which is the GIP auth pool (alm vs sre) — do NOT
    # wire the pool id here. Per-org derivation belongs here once real org
    # tenants exist; until then this single configured value is correct.
    registry_default_tenant: str = "devai"
    # On boot, re-publish every stored authored artifact (reconciles the
    # registry after a registry wipe or a devai redeploy). Off by default.
    registry_publish_on_boot: bool = False

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
    # Namespace the kagent controller serves A2A agents under
    # ({kagent_url}/api/a2a/{namespace}/{agent}); matches the kagent-agent-sync
    # targetNamespace. Only consulted when dispatching to a kagent-managed agent.
    kagent_default_namespace: str = "kagent-system"

    # --- LLM adapter ---
    # Single switch picks the default LLM backend; specialization YAMLs
    # override per-role via their `llm_provider:` field. Missing SDKs /
    # config degrade gracefully to "noop" — adapter pattern, identical
    # to the memory family. See src/devai/adapters/llm/.
    llm_provider: str = "anthropic"  # noop | anthropic | openai | gateway
    llm_noop_canned_text: str = "[noop response]"

    # Optional per-provider overrides (the existing anthropic_api_key /
    # openai_api_key / openai_model / claude_model fields above feed the
    # factory).
    anthropic_base_url: str = ""
    openai_base_url: str = ""
    openai_organization: str = ""

    # gateway — the solo.io agentgateway as the single LLM egress. DevAI
    # speaks OpenAI wire format to the gateway; the gateway routes to any
    # backend (Vertex Gemini/Claude, Anthropic direct, OpenAI, …) per its
    # own config, authenticating to Vertex via Workload Identity (GSA
    # agentgateway-llm, terraform-new/stacks/12-vertex). Model names are
    # gateway-side aliases, so swapping backends never touches DevAI.
    llm_gateway_base_url: str = ""  # e.g. http://ai-gateway.agentgateway-system.svc.cluster.local:8080/v1
    llm_gateway_api_key: str = ""  # optional; gateway-enforced auth token
    llm_gateway_model: str = ""  # default model alias the gateway resolves

    # --- vertex adapter ---
    # Vertex AI (Gemini) over REST with Application Default Credentials —
    # on GKE that's the pod's Workload Identity GSA (roles/aiplatform.user).
    # In-cluster traffic rides the PSC endpoint via the vertex-aiplatform
    # private DNS zone (tesserix-k8s terraform-new/stacks/12-vertex), so
    # inference never leaves the VPC. No API keys anywhere on this path.
    vertex_project: str = ""  # DEVAI_VERTEX_PROJECT — empty disables the backend
    vertex_location: str = "global"  # or a region, e.g. asia-south1
    vertex_gemini_model: str = "gemini-2.5-flash"
    vertex_embedding_model: str = "text-embedding-005"
    # Route Vertex calls through the devai-ai-gateway instead of dialing
    # aiplatform.googleapis.com directly. The gateway injects the API key
    # (GCP SM: prod-devai-vertex-api-key) and strips caller auth, so pods
    # need no Vertex credentials at all on this path.
    vertex_base_url: str = ""  # e.g. http://ai-gateway.agentgateway-system.svc.cluster.local:8080/vertex
    vertex_api_key: str = ""  # direct API-key auth (local dev); ADC is the keyless default
    gcp_secret_vertex_api_key: str = "prod-devai-vertex-api-key"

    # openrouter — OpenAI-compatible aggregator (hundreds of models, one key).
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_model: str = ""  # e.g. meta-llama/llama-3.3-70b-instruct

    # groq base URL (key/model fields live in the legacy provider block above).
    groq_base_url: str = "https://api.groq.com/openai/v1"

    # Runtime fallback: ordered comma-separated providers that pick up a
    # call when everything before them failed (each link uses its own
    # default model). Canonical order: "groq,openrouter,anthropic" behind a
    # vertex_gemini primary. Unconfigured links are skipped; empty disables.
    llm_fallback_provider: str = ""

    # --- per-ROLE model routing ---
    # Each role resolves to a model + a provider failover chain:
    #   configured primary (anthropic) → llm_role_chain_provider — the
    # gateway routes Claude on VERTEX server-side, so the SAME model id is
    # preserved down the chain (Fable 5 on Anthropic → Fable 5 on Vertex).
    # Unconfigured fallback links skip with a log; empty disables the link.
    llm_role_chain_provider: str = "gateway"
    # Frontend/UI implementation — intuitive, design-strong output.
    llm_model_dev_ui: str = "claude-fable-5"
    # Backend/API + DB + infra implementation — deepest coding model.
    llm_model_dev_api: str = "claude-opus-4-8"
    # Code review / security / QA verdicts.
    llm_model_review: str = "claude-sonnet-4-6"
    # Epics / stories / engineering plans.
    llm_model_planning: str = "claude-sonnet-4-6"
    # High-volume utility calls (clarity checks, heal diagnosis, runbooks,
    # governance compose) — capable but cheap.
    llm_model_utility: str = "claude-haiku-4-5-20251001"
    # Boardroom: many parallel debater calls → good but LOW-COST seats;
    # the moderator (synthesis + decision document) gets a stronger model.
    llm_model_boardroom_panel: str = "claude-haiku-4-5-20251001"
    llm_model_boardroom_moderator: str = "claude-sonnet-4-6"

    # Require interactive users to bring their own LLM connector: when true,
    # principals with no Settings LLM connector get a clear "configure your
    # connector" stub instead of silently riding the shared platform key.
    llm_require_user_connector: bool = False

    # Trial allowance that softens the strict mode above: users WITHOUT
    # their own connector may spend this many tokens on the platform chain
    # to try DevAI. Usage accrues permanently (Redis) — at exhaustion the
    # shared keys are auto-revoked for that user forever; only their own
    # Settings connector serves them afterwards. 0 disables trials
    # (strict mode then blocks immediately).
    llm_trial_token_budget: int = 0

    # Service principal for system/webhook/cron runs (SRE scans, scheduled
    # work, automation). When set to an email that owns a Settings LLM
    # connector, those runs resolve THAT connector through the same overlay
    # + gateway path as a human user — so even automation uses a tenant
    # connector instead of the raw shared platform keys. Empty → automation
    # uses the platform fallback chain.
    llm_system_principal: str = ""

    # --- LLM cost tiers ---
    # "provider:model" pairs resolved by resolve_llm_tier(); lets stages and
    # specializations ask for a cost class instead of naming a provider.
    llm_tier_light: str = ""  # e.g. vertex_gemini:gemini-3.1-flash-lite
    llm_tier_standard: str = ""  # e.g. vertex_gemini:gemini-2.5-flash
    llm_tier_heavy: str = ""  # e.g. anthropic:claude-sonnet-4-20250514
    llm_tier_frontier: str = ""  # e.g. openai:o3

    # --- Secrets adapter (per-user/per-tenant secret provisioning) ---
    # Backs the Settings capability. The Settings store keeps only secret
    # references; the adapter writes/reads the actual values. gcp_sm
    # auto-provisions into Google Secret Manager (needs write IAM on the
    # devai SA); env is read-only; noop refuses writes loudly. Unknown
    # provider / missing SDK / missing project degrades to noop.
    secrets_provider: str = "noop"  # noop | env | gcp_sm
    # GCP project for gcp_sm (falls back to gke_project / gcp_project).
    secrets_gcp_project: str = ""
    # Enable the Settings capability (per-user connectors + secrets API).
    settings_enabled: bool = True

    # --- Memory adapter (Agentic AI Memory) ---
    # Single switch picks the backend; the rest of DevAI talks only to
    # `devai.adapters.memory.MemoryAdapter`. Swap providers with one env var,
    # no code changes. Missing SDKs / config degrade gracefully to "noop".
    memory_provider: str = "redis"  # noop | redis | pgvector | mem0 | zep | hondo

    # Embedding provider for memory semantic search (pgvector). `auto` uses
    # OpenAI when DEVAI_OPENAI_API_KEY is set, otherwise disables embeddings
    # and pgvector degrades to keyword recall. `none` disables explicitly.
    # Dimensions must match the agent_memories.embedding column: vector(1536).
    embedding_provider: str = "auto"  # auto | openai | none
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 1536

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

    # --- Memory lifecycle maintenance (pgvector store) ---
    # Periodic pass run by the API server: purges expired episodic records,
    # decays relevance of idle ones, and merges near-duplicates. 0 disables.
    memory_maintenance_hours: int = 6
    memory_episodic_ttl_days: int = 90
    memory_decay_idle_days: int = 30
    memory_dedup_similarity: float = 0.95

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
