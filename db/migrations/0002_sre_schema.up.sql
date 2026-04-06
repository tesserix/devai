-- DevAI SRE Platform — PostgreSQL Schema
-- Stores all monitoring data, incidents, cost analysis, remediation actions,
-- and health check history for the SRE AI agents.

-- ============================================================================
-- MONITORED CLUSTERS — clusters/environments being watched
-- ============================================================================

CREATE TABLE sre_clusters (
    id              TEXT PRIMARY KEY,           -- e.g. "tesseract-prod-in-gke"
    name            TEXT NOT NULL,
    provider        TEXT NOT NULL,              -- gke | eks | aks | self-hosted
    project_id      TEXT,                       -- GCP project / AWS account
    region          TEXT,
    endpoint        TEXT,                       -- K8s API server URL
    namespaces      TEXT[] DEFAULT '{}',        -- namespaces to monitor
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================================
-- MONITORED APPS — services/apps being watched
-- ============================================================================

CREATE TABLE sre_apps (
    id              TEXT PRIMARY KEY,
    cluster_id      TEXT NOT NULL REFERENCES sre_clusters(id),
    namespace       TEXT NOT NULL,
    name            TEXT NOT NULL,              -- deployment/service name
    repo            TEXT,                       -- linked SCM repo (org/repo)
    kind            TEXT DEFAULT 'Deployment',  -- Deployment | StatefulSet | DaemonSet | KnativeService
    health_url      TEXT,                       -- /healthz endpoint
    metrics_url     TEXT,                       -- /metrics endpoint
    owner_team      TEXT,                       -- team responsible
    owner_users     TEXT[] DEFAULT '{}',        -- GitHub/GitLab usernames to assign issues to
    alert_channels  TEXT[] DEFAULT '{}',        -- slack channels, email
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_sre_apps_cluster ON sre_apps(cluster_id);
CREATE INDEX idx_sre_apps_repo ON sre_apps(repo);

-- ============================================================================
-- INCIDENTS — detected issues from monitoring
-- ============================================================================

CREATE TABLE sre_incidents (
    id              TEXT PRIMARY KEY,           -- ULID
    cluster_id      TEXT REFERENCES sre_clusters(id),
    app_id          TEXT REFERENCES sre_apps(id),
    severity        TEXT NOT NULL,              -- critical | high | medium | low | info
    category        TEXT NOT NULL,              -- performance | cost | availability | security | capacity | error
    title           TEXT NOT NULL,
    description     TEXT NOT NULL,
    evidence        JSONB DEFAULT '{}'::jsonb,  -- metrics, logs, screenshots

    -- Detection
    detected_by     TEXT NOT NULL,              -- agent name that found it
    detection_method TEXT,                      -- metric_threshold | log_pattern | anomaly | cost_spike

    -- SCM integration
    scm_issue_number INTEGER,                  -- GitHub/GitLab issue created
    scm_pr_number    INTEGER,                  -- PR with fix (if auto-remediated)
    scm_repo         TEXT,                     -- repo where issue/PR was created
    assigned_to      TEXT[] DEFAULT '{}',       -- users assigned

    -- Resolution
    status          TEXT NOT NULL DEFAULT 'open', -- open | investigating | mitigating | resolved | false_positive
    resolved_at     TIMESTAMPTZ,
    resolution_note TEXT,
    mttr_seconds    INTEGER,                   -- mean time to resolve

    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_incidents_status ON sre_incidents(status) WHERE status IN ('open', 'investigating');
CREATE INDEX idx_incidents_severity ON sre_incidents(severity);
CREATE INDEX idx_incidents_app ON sre_incidents(app_id);
CREATE INDEX idx_incidents_created ON sre_incidents(created_at DESC);

-- ============================================================================
-- HEALTH CHECKS — periodic health snapshots
-- ============================================================================

CREATE TABLE sre_health_checks (
    id              BIGSERIAL PRIMARY KEY,
    cluster_id      TEXT NOT NULL REFERENCES sre_clusters(id),
    app_id          TEXT REFERENCES sre_apps(id),
    check_type      TEXT NOT NULL,              -- pod_status | http_health | resource_usage | cert_expiry | db_connection
    status          TEXT NOT NULL,              -- healthy | degraded | unhealthy | unknown
    details         JSONB DEFAULT '{}'::jsonb,
    response_ms     INTEGER,
    checked_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_health_cluster ON sre_health_checks(cluster_id, checked_at DESC);
CREATE INDEX idx_health_app ON sre_health_checks(app_id, checked_at DESC);

-- Partition by month for efficient queries (PostgreSQL 12+)
-- In production, convert to partitioned table

-- ============================================================================
-- METRICS SNAPSHOTS — aggregated metrics for trending
-- ============================================================================

CREATE TABLE sre_metrics (
    id              BIGSERIAL PRIMARY KEY,
    cluster_id      TEXT NOT NULL REFERENCES sre_clusters(id),
    app_id          TEXT REFERENCES sre_apps(id),
    metric_name     TEXT NOT NULL,              -- cpu_usage | memory_usage | error_rate | latency_p99 | pod_restarts | request_rate
    metric_value    DOUBLE PRECISION NOT NULL,
    metric_unit     TEXT,                       -- percent | bytes | ms | count | req/s
    labels          JSONB DEFAULT '{}'::jsonb,  -- extra dimensions
    recorded_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_metrics_app ON sre_metrics(app_id, metric_name, recorded_at DESC);
CREATE INDEX idx_metrics_cluster ON sre_metrics(cluster_id, metric_name, recorded_at DESC);

-- ============================================================================
-- COST REPORTS — cloud cost tracking
-- ============================================================================

CREATE TABLE sre_cost_reports (
    id              BIGSERIAL PRIMARY KEY,
    cluster_id      TEXT REFERENCES sre_clusters(id),
    report_date     DATE NOT NULL,
    provider        TEXT NOT NULL,              -- gcp | aws | azure

    -- Cost breakdown
    total_cost_usd  NUMERIC(12,2) NOT NULL,
    compute_cost    NUMERIC(12,2) DEFAULT 0,
    storage_cost    NUMERIC(12,2) DEFAULT 0,
    network_cost    NUMERIC(12,2) DEFAULT 0,
    other_cost      NUMERIC(12,2) DEFAULT 0,

    -- Resource details
    breakdown       JSONB DEFAULT '{}'::jsonb,  -- per-namespace, per-service costs
    recommendations JSONB DEFAULT '{}'::jsonb,  -- cost saving suggestions

    -- Trend
    daily_change_pct NUMERIC(6,2),              -- % change from previous day
    monthly_forecast NUMERIC(12,2),             -- projected monthly cost

    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_cost_date ON sre_cost_reports(report_date DESC);
CREATE UNIQUE INDEX idx_cost_unique ON sre_cost_reports(cluster_id, report_date, provider);

-- ============================================================================
-- REMEDIATION ACTIONS — what the SRE agent did to fix issues
-- ============================================================================

CREATE TABLE sre_remediations (
    id              TEXT PRIMARY KEY,           -- ULID
    incident_id     TEXT NOT NULL REFERENCES sre_incidents(id),
    action_type     TEXT NOT NULL,              -- create_issue | create_pr | scale_up | restart_pod | rollback | alert
    description     TEXT NOT NULL,

    -- What was done
    target          TEXT,                       -- deployment name, PR URL, issue URL
    payload         JSONB DEFAULT '{}'::jsonb,  -- action-specific data

    -- Outcome
    status          TEXT NOT NULL DEFAULT 'pending', -- pending | executed | failed | rolled_back
    executed_at     TIMESTAMPTZ,
    result          TEXT,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_remediation_incident ON sre_remediations(incident_id);

-- ============================================================================
-- SRE SCAN RUNS — each scheduled monitoring run
-- ============================================================================

CREATE TABLE sre_scan_runs (
    id              TEXT PRIMARY KEY,           -- ULID
    cluster_id      TEXT NOT NULL REFERENCES sre_clusters(id),
    trigger         TEXT NOT NULL DEFAULT 'cron', -- cron | manual | alert
    status          TEXT NOT NULL DEFAULT 'running', -- running | completed | failed

    -- Results summary
    incidents_found INTEGER DEFAULT 0,
    apps_checked    INTEGER DEFAULT 0,
    checks_passed   INTEGER DEFAULT 0,
    checks_failed   INTEGER DEFAULT 0,

    -- Agent timings
    agent_timings   JSONB DEFAULT '{}'::jsonb,

    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMPTZ
);

CREATE INDEX idx_scan_runs_cluster ON sre_scan_runs(cluster_id, started_at DESC);

-- ============================================================================
-- VIEWS
-- ============================================================================

-- Current cluster health overview
CREATE VIEW v_sre_cluster_health AS
SELECT
    c.id AS cluster_id,
    c.name AS cluster_name,
    COUNT(DISTINCT a.id) AS total_apps,
    COUNT(DISTINCT i.id) FILTER (WHERE i.status IN ('open', 'investigating')) AS open_incidents,
    COUNT(DISTINCT i.id) FILTER (WHERE i.severity = 'critical' AND i.status = 'open') AS critical_incidents,
    (SELECT total_cost_usd FROM sre_cost_reports cr
     WHERE cr.cluster_id = c.id ORDER BY report_date DESC LIMIT 1) AS latest_daily_cost
FROM sre_clusters c
LEFT JOIN sre_apps a ON a.cluster_id = c.id AND a.is_active
LEFT JOIN sre_incidents i ON i.cluster_id = c.id
WHERE c.is_active
GROUP BY c.id;

-- App reliability summary
CREATE VIEW v_sre_app_reliability AS
SELECT
    a.id AS app_id,
    a.name,
    a.namespace,
    a.repo,
    COUNT(i.id) AS total_incidents_30d,
    COUNT(i.id) FILTER (WHERE i.severity IN ('critical', 'high')) AS severe_incidents_30d,
    AVG(i.mttr_seconds) AS avg_mttr_seconds,
    COUNT(hc.id) FILTER (WHERE hc.status = 'healthy') * 100.0 /
        NULLIF(COUNT(hc.id), 0) AS health_pct_7d
FROM sre_apps a
LEFT JOIN sre_incidents i ON i.app_id = a.id AND i.created_at > NOW() - INTERVAL '30 days'
LEFT JOIN sre_health_checks hc ON hc.app_id = a.id AND hc.checked_at > NOW() - INTERVAL '7 days'
WHERE a.is_active
GROUP BY a.id;
