-- DevAI ALM Platform — PostgreSQL Schema
-- Stores the complete lifecycle of every pipeline run, agent execution,
-- A2A communication, audit trail, and agent memory.
--
-- Design principles:
--   1. Nothing is ever deleted — soft-delete only, full audit trail
--   2. Every agent action is recorded with timing and outcome
--   3. A2A messages form a queryable communication graph
--   4. Agent memory supports semantic search (pgvector)
--   5. Schema changes are tracked via Liquibase-style changelog

-- Enable required extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pgvector";  -- for semantic memory search

-- ============================================================================
-- PIPELINE RUNS — the central record of every ALM execution
-- ============================================================================

CREATE TABLE pipeline_runs (
    id              TEXT PRIMARY KEY,           -- ULID
    repo_full_name  TEXT NOT NULL,              -- e.g. "tesserix/Home-Chef-App"
    trigger_type    TEXT NOT NULL,              -- github_issue | project_card | cli | webhook
    trigger_ref     TEXT NOT NULL DEFAULT '',   -- issue number, card ID, etc.
    requirements    TEXT NOT NULL DEFAULT '',   -- raw requirements text (the full input)
    stage           TEXT NOT NULL DEFAULT 'triggered',
    branch_name     TEXT,
    pr_number       INTEGER,
    review_iteration INTEGER NOT NULL DEFAULT 0,

    -- Outcomes
    security_decision TEXT,                    -- pass | pass_with_warnings | block
    db_decision       TEXT,                    -- safe | review_needed | blocked | not_applicable
    build_status      TEXT,
    deploy_status     TEXT,
    deploy_environment TEXT DEFAULT 'production',

    -- Metadata
    error           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMPTZ,

    -- Governance snapshot (CLAUDE.md at time of run)
    governance_snapshot TEXT
);

CREATE INDEX idx_runs_repo ON pipeline_runs(repo_full_name);
CREATE INDEX idx_runs_stage ON pipeline_runs(stage);
CREATE INDEX idx_runs_created ON pipeline_runs(created_at DESC);
CREATE INDEX idx_runs_trigger ON pipeline_runs(trigger_type, trigger_ref);

-- ============================================================================
-- AGENT EXECUTIONS — every agent run within a pipeline
-- ============================================================================

CREATE TABLE agent_executions (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    run_id          TEXT NOT NULL REFERENCES pipeline_runs(id),
    agent_name      TEXT NOT NULL,              -- e.g. "security_expert"
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending | running | completed | failed | waiting_approval

    -- Timing
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    duration_ms     INTEGER,

    -- Input/Output
    input_summary   TEXT,                      -- what the agent received
    output_summary  TEXT,                      -- what the agent produced
    output_data     JSONB DEFAULT '{}'::jsonb,  -- structured output

    -- Error tracking
    error_message   TEXT,
    retry_count     INTEGER NOT NULL DEFAULT 0,

    -- LLM usage
    provider        TEXT,                      -- claude | openai | groq
    model           TEXT,                      -- claude-sonnet-4, o3, llama-3.3-70b
    tokens_input    INTEGER DEFAULT 0,
    tokens_output   INTEGER DEFAULT 0,
    llm_cost_usd    NUMERIC(10,6) DEFAULT 0,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_agent_exec_run ON agent_executions(run_id);
CREATE INDEX idx_agent_exec_agent ON agent_executions(agent_name);
CREATE INDEX idx_agent_exec_status ON agent_executions(status);

-- ============================================================================
-- A2A MESSAGES — agent-to-agent communication log
-- ============================================================================

CREATE TABLE a2a_messages (
    id              TEXT PRIMARY KEY,           -- ULID
    run_id          TEXT NOT NULL REFERENCES pipeline_runs(id),
    from_agent      TEXT NOT NULL,
    to_agent        TEXT NOT NULL,
    message_type    TEXT NOT NULL,              -- request | response | notification | handoff | escalation | clarification
    subject         TEXT NOT NULL,
    body            TEXT NOT NULL,
    payload         JSONB DEFAULT '{}'::jsonb,
    in_reply_to     TEXT REFERENCES a2a_messages(id),

    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_a2a_run ON a2a_messages(run_id);
CREATE INDEX idx_a2a_from ON a2a_messages(from_agent);
CREATE INDEX idx_a2a_to ON a2a_messages(to_agent);
CREATE INDEX idx_a2a_type ON a2a_messages(message_type);
CREATE INDEX idx_a2a_reply ON a2a_messages(in_reply_to);

-- ============================================================================
-- AGENT MEMORY — persistent cross-run learning
-- ============================================================================

CREATE TABLE agent_memories (
    id              TEXT PRIMARY KEY,           -- ULID
    agent           TEXT NOT NULL,              -- which agent stored this
    repo            TEXT NOT NULL DEFAULT 'global',
    memory_type     TEXT NOT NULL,              -- episodic | semantic | procedural
    content         TEXT NOT NULL,
    tags            TEXT[] DEFAULT '{}',
    metadata        JSONB DEFAULT '{}'::jsonb,

    -- Retrieval scoring
    relevance_score REAL NOT NULL DEFAULT 1.0,
    access_count    INTEGER NOT NULL DEFAULT 0,
    last_accessed   TIMESTAMPTZ,

    -- Semantic search (pgvector)
    embedding       vector(1536),              -- OpenAI text-embedding-3-small dimension

    -- Lifecycle
    expires_at      TIMESTAMPTZ,               -- NULL = never expires
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_memory_agent ON agent_memories(agent);
CREATE INDEX idx_memory_repo ON agent_memories(repo);
CREATE INDEX idx_memory_type ON agent_memories(memory_type);
CREATE INDEX idx_memory_tags ON agent_memories USING GIN(tags);
CREATE INDEX idx_memory_active ON agent_memories(is_active) WHERE is_active = TRUE;

-- Semantic similarity search index (HNSW for fast ANN)
CREATE INDEX idx_memory_embedding ON agent_memories
    USING hnsw (embedding vector_cosine_ops)
    WHERE embedding IS NOT NULL;

-- ============================================================================
-- AUDIT LOG — immutable record of every action taken
-- ============================================================================

CREATE TABLE audit_log (
    id              BIGSERIAL PRIMARY KEY,
    run_id          TEXT REFERENCES pipeline_runs(id),
    agent_name      TEXT,
    action          TEXT NOT NULL,              -- e.g. "created_issue", "approved_gate", "blocked_security"
    entity_type     TEXT,                       -- issue | pr | branch | migration | deployment
    entity_ref      TEXT,                       -- issue #, PR #, branch name, etc.
    details         JSONB DEFAULT '{}'::jsonb,

    -- Who/what triggered this
    actor           TEXT NOT NULL,              -- agent name or user login
    actor_type      TEXT NOT NULL DEFAULT 'agent',  -- agent | user | system

    -- Immutable timestamp
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Audit log is append-only — no UPDATE or DELETE allowed (enforced by REVOKE)
CREATE INDEX idx_audit_run ON audit_log(run_id);
CREATE INDEX idx_audit_agent ON audit_log(agent_name);
CREATE INDEX idx_audit_action ON audit_log(action);
CREATE INDEX idx_audit_entity ON audit_log(entity_type, entity_ref);
CREATE INDEX idx_audit_created ON audit_log(created_at DESC);

-- ============================================================================
-- APPROVAL GATES — human approval records
-- ============================================================================

CREATE TABLE approval_gates (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    run_id          TEXT NOT NULL REFERENCES pipeline_runs(id),
    gate_name       TEXT NOT NULL,              -- createPR | review | testing | deploy | security
    agent_name      TEXT NOT NULL,              -- which agent is waiting
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | rejected | expired

    -- Decision
    decided_by      TEXT,                       -- user login who approved/rejected
    decided_at      TIMESTAMPTZ,
    reason          TEXT,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ DEFAULT NOW() + INTERVAL '1 hour'
);

CREATE INDEX idx_gates_run ON approval_gates(run_id);
CREATE INDEX idx_gates_status ON approval_gates(status) WHERE status = 'pending';

-- ============================================================================
-- DB MIGRATIONS TRACKER — tracks schema changes made by DB Engineer
-- ============================================================================

CREATE TABLE db_migration_audit (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    run_id          TEXT NOT NULL REFERENCES pipeline_runs(id),
    repo            TEXT NOT NULL,

    -- Migration details
    migration_type  TEXT NOT NULL,              -- liquibase | alembic | prisma | raw_sql
    migration_file  TEXT NOT NULL,              -- path to the migration file
    migration_hash  TEXT,                       -- SHA256 of migration content
    direction       TEXT NOT NULL DEFAULT 'up', -- up | down (rollback)

    -- Safety analysis
    is_destructive  BOOLEAN NOT NULL DEFAULT FALSE,
    tables_created  TEXT[] DEFAULT '{}',
    tables_modified TEXT[] DEFAULT '{}',
    tables_dropped  TEXT[] DEFAULT '{}',        -- SHOULD BE EMPTY in normal ops
    columns_added   TEXT[] DEFAULT '{}',
    columns_dropped TEXT[] DEFAULT '{}',

    -- Status
    status          TEXT NOT NULL DEFAULT 'generated', -- generated | applied | rolled_back | rejected
    applied_by      TEXT,
    applied_at      TIMESTAMPTZ,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_db_migration_run ON db_migration_audit(run_id);
CREATE INDEX idx_db_migration_repo ON db_migration_audit(repo);

-- ============================================================================
-- SECURITY SCAN RESULTS — detailed findings from Security Expert
-- ============================================================================

CREATE TABLE security_findings (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    run_id          TEXT NOT NULL REFERENCES pipeline_runs(id),
    repo            TEXT NOT NULL,
    pr_number       INTEGER,

    -- Finding details
    scanner         TEXT NOT NULL,              -- sast | sca | secrets | container | owasp
    severity        TEXT NOT NULL,              -- critical | high | medium | low | info
    tool            TEXT,                       -- bandit, gosec, npm audit, etc.

    file_path       TEXT,
    line_number     INTEGER,
    issue           TEXT NOT NULL,
    cve_id          TEXT,
    cwe_id          TEXT,

    -- Resolution
    status          TEXT NOT NULL DEFAULT 'open', -- open | fixed | false_positive | accepted_risk
    resolved_by     TEXT,
    resolved_at     TIMESTAMPTZ,
    resolution_note TEXT,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_security_run ON security_findings(run_id);
CREATE INDEX idx_security_severity ON security_findings(severity);
CREATE INDEX idx_security_status ON security_findings(status) WHERE status = 'open';

-- ============================================================================
-- PIPELINE CONFIGURATION — per-repo settings
-- ============================================================================

CREATE TABLE pipeline_config (
    repo            TEXT PRIMARY KEY,           -- "tesserix/my-repo" or "default"
    auto_mode       BOOLEAN NOT NULL DEFAULT FALSE,
    gates           JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- Model preferences
    claude_model    TEXT DEFAULT 'claude-sonnet-4-20250514',
    openai_model    TEXT DEFAULT 'o3',
    groq_model      TEXT DEFAULT 'llama-3.3-70b-versatile',

    -- Limits
    max_review_iterations INTEGER DEFAULT 3,
    agent_timeout_seconds INTEGER DEFAULT 900,

    -- Governance
    claude_md_content TEXT,
    claude_md_version INTEGER DEFAULT 0,

    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_by      TEXT
);

-- ============================================================================
-- VIEWS — useful aggregations
-- ============================================================================

-- Recent pipeline activity per repo
CREATE VIEW v_recent_activity AS
SELECT
    r.id,
    r.repo_full_name,
    r.stage,
    r.trigger_type,
    r.created_at,
    r.completed_at,
    EXTRACT(EPOCH FROM (COALESCE(r.completed_at, NOW()) - r.created_at)) AS duration_seconds,
    COUNT(DISTINCT ae.id) AS agent_count,
    COUNT(DISTINCT a2a.id) AS message_count,
    SUM(ae.tokens_input + ae.tokens_output) AS total_tokens,
    SUM(ae.llm_cost_usd) AS total_cost_usd
FROM pipeline_runs r
LEFT JOIN agent_executions ae ON ae.run_id = r.id
LEFT JOIN a2a_messages a2a ON a2a.run_id = r.id
GROUP BY r.id
ORDER BY r.created_at DESC;

-- Agent performance stats
CREATE VIEW v_agent_stats AS
SELECT
    agent_name,
    COUNT(*) AS total_executions,
    COUNT(*) FILTER (WHERE status = 'completed') AS successes,
    COUNT(*) FILTER (WHERE status = 'failed') AS failures,
    AVG(duration_ms) AS avg_duration_ms,
    SUM(tokens_input + tokens_output) AS total_tokens,
    SUM(llm_cost_usd) AS total_cost_usd
FROM agent_executions
GROUP BY agent_name;

-- Security posture per repo
CREATE VIEW v_security_posture AS
SELECT
    repo,
    COUNT(*) AS total_findings,
    COUNT(*) FILTER (WHERE severity = 'critical') AS critical,
    COUNT(*) FILTER (WHERE severity = 'high') AS high,
    COUNT(*) FILTER (WHERE status = 'open') AS open_findings,
    COUNT(*) FILTER (WHERE status = 'fixed') AS fixed
FROM security_findings
GROUP BY repo;
