"""Analytics — read-only aggregates over the pipeline runtime + telemetry.

The analytics surface powers the dashboard `/analytics` page. It aggregates:

  - run-level stats from the live pipeline runtime (Redis-persisted Fiber
    tasks): totals, success rate, durations, runs/day, per-blueprint, per-stage
  - agent / LLM token + cost rollups from Postgres `agent_executions`
    (best-effort — empty when the DB is unreachable)
  - a best-effort SRE summary from the shared `devai_db` SRE views
  - telemetry / OTel-collector health from the telemetry adapter + Prometheus
    reachability via the observability adapter

Everything is read-only and degrades cleanly: a missing pipeline service, DB,
or telemetry adapter yields empty/null sections, never a 500.
"""

from devai.analytics.routes import router

__all__ = ["router"]
