-- Reference schema (production schema lives in tesserix-k8s db-schema-bootstrap).
-- agent_evals: quality scores per run/stage for the analytics Evals view.
CREATE TABLE IF NOT EXISTS agent_evals (
    id             BIGSERIAL PRIMARY KEY,
    run_id         TEXT,
    stage          TEXT,
    agent_name     TEXT,
    evaluator      TEXT NOT NULL,
    score          DOUBLE PRECISION NOT NULL DEFAULT 0,
    passed         BOOLEAN NOT NULL DEFAULT FALSE,
    triggered_by   TEXT,
    detail         JSONB DEFAULT '{}'::jsonb,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_evals_run ON agent_evals(run_id);
CREATE INDEX IF NOT EXISTS idx_evals_user ON agent_evals(triggered_by, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_evals_evaluator ON agent_evals(evaluator, created_at DESC);
