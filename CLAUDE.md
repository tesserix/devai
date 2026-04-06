# Claude Reference Guide — DevAI

DevAI is an AI-powered Application Lifecycle Management (ALM) + SRE monitoring platform built with LangGraph, LangChain, and LangSmith. It orchestrates 14 ALM agents and 7 SRE agents to automate the full software development lifecycle — from requirement ingestion to production deployment and monitoring.

---

## Critical Rules

### 1. Git Identity

**Always** configure before any commit or push:

```bash
git config user.name "sam123ben"
git config user.email "samyak.rout@gmail.com"
```

### 2. No AI References

**NEVER** include Claude, Copilot, Anthropic, AI tool, or `Co-Authored-By` mentions in:
- Commit messages
- PR titles or descriptions
- Code comments
- Changelog entries
- Any file content

### 3. CI Build & Deploy Procedure (MUST FOLLOW)

The `tesserix` GitHub org has **limited Actions minutes** for private repos.
Every CI build requires a **public->build->private** cycle:

```bash
# Step 1: Make repo public
gh repo edit tesserix/devai --visibility public --accept-visibility-change-consequences

# Step 2: Push code (triggers CI automatically) OR trigger manually
git push origin main
# OR manually trigger specific workflows:
gh workflow run ci.yaml --repo tesserix/devai --ref main
gh workflow run dashboard-build.yaml --repo tesserix/devai --ref main
gh workflow run sre-dashboard-build.yaml --repo tesserix/devai --ref main

# Step 3: Wait for CI to complete — check status
gh run list --repo tesserix/devai --limit 5
gh run view <run-id> --repo tesserix/devai

# Step 4: Once ALL CI passes (all green), make repo private again
gh repo edit tesserix/devai --visibility private --accept-visibility-change-consequences
```

**Important:**
- Always verify CI is fully complete before making private
- If CI fails, fix the issue, push again (repo stays public), wait for green, then make private
- Never leave repos public overnight — always toggle back

### 4. No Manual kubectl apply — ArgoCD Only

**NEVER** use `kubectl apply`, `kubectl create`, `kubectl patch`, `kubectl edit`, or `kubectl set` to deploy or modify cluster resources. All changes go through ArgoCD via the `tesserix-k8s` repo.

### 5. SQL Scripts & Database Schemas

**NEVER** create new SQL migration files in `db/migrations/` directly. All database schemas MUST live in the `tesserix-k8s` infrastructure repo at:
```
tesserix-k8s/charts/apps/db-schema-bootstrap/schemas/devai/devai_db/
```

The existing `db/migrations/` files are reference schemas only. Production schemas are managed by the `db-schema-bootstrap` CronJob in `tesserix-k8s`.

This repo should only contain SQLAlchemy/asyncpg ORM logic in `src/devai/services/database.py` — never raw `.sql` files.

---

## Project Structure

```
devai/
├── src/devai/                  # Python source (main package)
│   ├── agents/                 # 13 ALM pipeline agents
│   ├── sre/                    # SRE monitoring subsystem
│   │   ├── agents/             # 7 SRE agents
│   │   ├── graph/              # SRE orchestrator
│   │   ├── tools/              # kubectl wrapper tools
│   │   └── server.py           # SRE FastAPI app (port 8090)
│   ├── graph/                  # LangGraph orchestration
│   │   ├── orchestrator.py     # ALM pipeline (14 nodes)
│   │   ├── state.py            # ALMState TypedDict
│   │   └── a2a.py              # Agent-to-agent messaging bus
│   ├── chat/                   # LangChain chat agent
│   │   ├── agent.py            # ChatAnthropic + 15 tools
│   │   └── routes.py           # REST, SSE, WebSocket endpoints
│   ├── tools/                  # Agent tool implementations
│   ├── scm/                    # Multi-SCM abstraction layer
│   ├── providers/              # LLM provider wrappers
│   ├── services/               # Database, memory, resilience, tracing
│   ├── webhook/                # GitHub/GitLab/ADO webhook handlers
│   ├── config.py               # Pydantic Settings (env: DEVAI_*)
│   └── models.py               # Shared Pydantic models
├── dashboard/                  # ALM dashboard (Next.js, port 3100)
├── sre-dashboard/              # SRE dashboard (Next.js, port 3200)
├── helm/devai/                 # Helm chart for K8s deployment
├── db/migrations/              # Reference SQL schemas (DO NOT ADD)
├── .github/workflows/          # CI/CD pipelines
├── Dockerfile                  # ALM service image
├── Dockerfile.sre              # SRE service image
└── pyproject.toml              # Python package config
```

---

## Architecture

### LLM Providers

| Provider | Model | Used By |
|----------|-------|---------|
| **Groq** | `llama-3.3-70b-versatile` | DocumentAnalyzer, TechDetector, RequirementsAnalyst, CIMonitor, ReleaseManager |
| **Claude (Anthropic)** | `claude-sonnet-4` | EngineeringManager, SeniorDeveloper, DBEngineer, SecurityExpert, QATester, InfraProvisioner, ChatAgent, IncidentResponder |
| **OpenAI** | `o3` | ProductDirector, StaffReviewer (Codex sandbox) |

### ALM Pipeline — 14 LangGraph Nodes

```
ingest_documents → detect_tech_stack → analyze_requirements
  → create_epic → create_stories → create_plan
  → implement_code → db_engineering → review_code ←→ (loop max 3)
  → security_scan ←→ (hard gate: block → re-implement)
  → monitor_build → run_tests ←→ (loop max 2)
  → provision_infra → deploy_release
```

Each node has:
- 15-minute timeout protection
- State checkpointing at boundaries
- Agent memory injection (cross-run learning)
- A2A message persistence to Redis
- LangSmith tracing (@traceable decorators)

### SRE Pipeline — 5 LangGraph Nodes

```
discover → monitor_parallel (5 agents in asyncio.gather)
  → correlate → respond → learn
```

Runs autonomously on a 5-minute cron schedule. No manual configuration needed — the Discovery Agent auto-maps all K8s namespaces, services, and dependencies.

### A2A (Agent-to-Agent) Communication

All agents communicate via `A2ABus` with 6 message types:
- `request` / `response` — structured ask/answer
- `notification` — fire-and-forget alerts
- `handoff` — transfer ownership of a task
- `escalation` — flag blockers to upstream agents
- `broadcast` — announce to all agents

Messages flow through `ALMState["a2a_messages"]` and persist to Redis + PostgreSQL.

### Multi-SCM Support

Abstract `SCMClient` with concrete implementations:
- **GitHub** — GitHub App, PAT, OAuth auth methods
- **GitLab** — PAT, OAuth, Merge Request API translation
- **Azure DevOps** — Basic Auth PAT, Work Items as issues

Factory: `create_scm_client(config)` in `src/devai/scm/factory.py`

### Agent Memory System

Redis-backed with 3 memory types:
- **Episodic** — what happened in past runs
- **Semantic** — learned patterns and repo conventions
- **Procedural** — how-to knowledge for specific tasks

Memory persists to PostgreSQL via pgvector for semantic search.

---

## Services & Dependencies

| Service | Purpose | Connection |
|---------|---------|------------|
| **PostgreSQL + pgvector** | Lifecycle persistence, semantic search | `DEVAI_DATABASE_URL` |
| **Redis** | Hot state, locks, sessions, pub/sub, memory cache | `DEVAI_REDIS_URL` |
| **NATS JetStream** | Async event bus (legacy mode) | `DEVAI_NATS_URL` |
| **LangSmith** | LLM tracing & observability | `DEVAI_LANGCHAIN_API_KEY` |

### Database Schema (PostgreSQL)

**ALM tables:** `pipeline_runs`, `agent_executions`, `a2a_messages`, `agent_memories`, `audit_log`, `approval_gates`, `db_migration_audit`, `security_findings`, `pipeline_config`

**SRE tables:** `sre_clusters`, `sre_apps`, `sre_incidents`, `sre_health_checks`, `sre_metrics`, `sre_cost_reports`, `sre_remediations`, `sre_scan_runs`

Extensions: `uuid-ossp`, `pgvector`

---

## Configuration

All settings via environment variables prefixed `DEVAI_`. Defined in `src/devai/config.py` using Pydantic Settings.

Key settings:
- `DEVAI_DATABASE_URL` — PostgreSQL connection string
- `DEVAI_REDIS_URL` — Redis connection (default: `redis://localhost:6379`)
- `DEVAI_NATS_URL` — NATS server (default: `nats://localhost:4222`)
- `DEVAI_SCM_PROVIDER` — `github` | `gitlab` | `azure_devops`
- `DEVAI_SCM_AUTH_METHOD` — `github_app` | `pat` | `oauth` | `ado_pat` | `gitlab_token`
- `DEVAI_ANTHROPIC_API_KEY` — Claude API key
- `DEVAI_OPENAI_API_KEY` — OpenAI API key
- `DEVAI_GROQ_API_KEY` — Groq API key (also via GCP Secret Manager: `DEVAI_GCP_SECRET_GROQ_API_KEY`)
- `DEVAI_GROQ_MODEL` — default: `llama-3.3-70b-versatile`
- `DEVAI_CLAUDE_MODEL` — default: `claude-sonnet-4-20250514`
- `DEVAI_LANGCHAIN_TRACING_V2` — enable LangSmith tracing
- `DEVAI_LANGCHAIN_API_KEY` — LangSmith API key
- `DEVAI_LANGCHAIN_PROJECT` — default: `devai`
- `DEVAI_KEYCLOAK_URL`, `DEVAI_KEYCLOAK_REALM`, `DEVAI_KEYCLOAK_CLIENT_ID` — OIDC auth
- `DEVAI_MAX_REVIEW_ITERATIONS` — default: 3
- `DEVAI_PIPELINE_LABEL` — default: `devai:automate`

---

## Entry Points

| Command | Module | Purpose |
|---------|--------|---------|
| `devai` | `devai.cli.commands:app` | CLI for ALM pipeline operations |
| `devai-sre` | `devai.sre.server:create_sre_app` | SRE FastAPI server |
| FastAPI app | `devai.webhook.app` | ALM webhook + chat API server |

---

## CI/CD Workflows

| Workflow | File | Triggers | What It Builds |
|----------|------|----------|----------------|
| ALM CI | `ci.yaml` | push to main | Lint → Test → Build → Push `ghcr.io/tesserix/devai` → Trivy scan |
| SRE CI | `sre-build.yaml` | push to main | Lint → Test → Build → Push `ghcr.io/tesserix/devai-sre` |
| Dashboard CI | `dashboard-build.yaml` | push to main | Build → Push `ghcr.io/tesserix/devai-dashboard` |
| SRE Dashboard CI | `sre-dashboard-build.yaml` | push to main | Build → Push `ghcr.io/tesserix/devai-sre-dashboard` |
| Release | `release.yaml` | tag `v*` | Builds all 4 images, creates GitHub Release |

All workflows use:
- `FORCE_JAVASCRIPT_ACTIONS_TO_NODE24=true`
- WIF (Workload Identity Federation) for keyless GCP auth
- `github/codeql-action/upload-sarif@v4` for security scanning
- `continue-on-error: true` on lint jobs (TC001/E501 are acceptable)

---

## Dashboards

### ALM Dashboard (port 3100)
- Next.js 15, standalone output
- Tabs: Overview, Agents, A2A Messages, Chat, Config
- Components: `pipeline-flow`, `agent-card`, `run-list`, `a2a-feed`, `chat-panel`, `approval-banner`, `trigger-dialog`
- API client at `dashboard/src/lib/api.ts`

### SRE Dashboard (port 3200)
- Next.js 15, standalone output
- Components: `cluster-overview`, `incident-feed`, `scan-history`
- API client at `sre-dashboard/src/lib/api.ts`

---

## Chat Agent

LangChain-powered conversational interface (`src/devai/chat/agent.py`):
- Uses `ChatAnthropic` with ReAct tool-calling loop (max 8 iterations)
- 15 tools: 10 data query tools + 5 SCM repo tools
- Session-based conversation history (30-message trim)
- Endpoints: `POST /chat/api/message`, `POST /chat/api/message/stream` (SSE), `WebSocket /chat/ws`

---

## Resilience Patterns

Defined in `src/devai/services/resilience.py`:
- `retry_async` — exponential backoff with configurable max retries
- `with_timeout` — async timeout wrapper
- `CircuitBreaker` — prevents cascading failures
- `StateCheckpoint` — saves/restores pipeline state for resume-from-failure

---

## GCP & GitHub

- **GCP Project:** `tesseracthub-480811`
- **GCP Region:** `asia-south1`
- **GKE Cluster:** `tesseract-prod-in-gke`
- **GitHub Org:** `tesserix`
- **GHCR Images:** `ghcr.io/tesserix/devai`, `ghcr.io/tesserix/devai-sre`, `ghcr.io/tesserix/devai-dashboard`, `ghcr.io/tesserix/devai-sre-dashboard`

### Helm Deployment

```
helm/devai/
├── Chart.yaml
├── values.yaml / values-prod.yaml
└── templates/
    ├── deployment.yaml
    ├── service.yaml
    ├── serviceaccount.yaml
    ├── configmap.yaml
    ├── externalsecret.yaml    # DEVAI_DATABASE_URL, DEVAI_GROQ_API_KEY, DEVAI_LANGCHAIN_API_KEY from GCP SM
    └── virtualservice.yaml    # Istio routing
```

ArgoCD apps should be created in `tesserix-k8s/argocd/prod/apps/devai/`.

---

## Development

```bash
# Install dependencies
pip install -e ".[dev,docs]"

# Run ALM server locally
uvicorn devai.webhook.app:app --reload --port 8080

# Run SRE server locally
uvicorn devai.sre.server:app --reload --port 8090

# Run ALM dashboard
cd dashboard && npm install && npm run dev  # port 3100

# Run SRE dashboard
cd sre-dashboard && npm install && npm run dev  # port 3200

# Lint
ruff check src/

# Tests
pytest tests/ -v
```
