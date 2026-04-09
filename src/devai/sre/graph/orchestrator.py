"""SRE LangGraph Orchestrator — fully autonomous monitoring pipeline.

Runs continuously on a schedule (default every 5 minutes). Each scan:

    discovery ─── (auto-discovers all services, deps, topology)
      ↓
    ┌─────────────────────────────────┐
    │  Parallel monitoring agents     │
    │  ├── infra_monitor              │
    │  ├── log_analyzer               │
    │  ├── perf_monitor               │
    │  ├── cost_analyzer              │
    │  └── capacity_planner           │
    └─────────────────────────────────┘
      ↓
    correlate_findings ── (de-duplicate, correlate, prioritize)
      ↓
    incident_responder ── (create issues/PRs on GitHub)
      ↓
    learn ── (store learnings in memory for future scans)
      ↓
    END

The system is fully autonomous — no configuration needed beyond
cluster access (kubectl). The Discovery Agent maps everything.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any, TypedDict

from langgraph.graph import END, StateGraph

from devai.services.tracing import traceable_if_enabled

if TYPE_CHECKING:
    from devai.config import Settings

logger = logging.getLogger(__name__)


class SREState(TypedDict, total=False):
    """State flowing through the SRE LangGraph."""

    scan_id: str
    cluster_id: str
    trigger: str  # cron | manual | alert

    # Discovery output
    namespaces: list[str]
    apps: list[dict[str, Any]]
    services: list[dict[str, Any]]
    topology: dict[str, Any]

    # Agent findings (parallel)
    infra_findings: list[dict[str, Any]]
    log_findings: list[dict[str, Any]]
    perf_findings: list[dict[str, Any]]
    cost_findings: list[dict[str, Any]]
    capacity_findings: list[dict[str, Any]]

    # Correlated
    all_findings: list[dict[str, Any]]

    # Results
    incidents_created: int
    prs_created: int
    agent_timings: dict[str, float]

    error: str | None


class SREOrchestrator:
    """Fully autonomous SRE monitoring pipeline."""

    def __init__(self, config: Settings, database: Any = None) -> None:
        self.config = config
        self.db = database
        self._graph = self._build_graph()

    def _build_graph(self) -> Any:
        graph = StateGraph(SREState)

        graph.add_node("discover", self._node_discover)
        graph.add_node("monitor_parallel", self._node_monitor_parallel)
        graph.add_node("correlate", self._node_correlate)
        graph.add_node("respond", self._node_respond)
        graph.add_node("learn", self._node_learn)

        graph.set_entry_point("discover")
        graph.add_edge("discover", "monitor_parallel")
        graph.add_edge("monitor_parallel", "correlate")
        graph.add_edge("correlate", "respond")
        graph.add_edge("respond", "learn")
        graph.add_edge("learn", END)

        return graph.compile()

    # --- Nodes ---

    @traceable_if_enabled(name="sre.discover", run_type="chain")
    async def _node_discover(self, state: SREState) -> dict[str, Any]:
        """Auto-discover all services on the cluster."""
        start = time.monotonic()

        from devai.sre.agents.discovery_agent import DiscoveryAgent

        # Try to load memory for context
        memory = None
        try:
            import redis.asyncio as redis_lib

            from devai.services.memory import AgentMemory

            r = redis_lib.from_url(self.config.redis_url, decode_responses=True)
            memory = AgentMemory(r)
        except Exception:
            pass

        agent = DiscoveryAgent(self.config, memory=memory)
        result = await agent.run()

        # Assign stable IDs and persist the discovered apps so the dashboard
        # views (v_sre_app_reliability, v_sre_cluster_health) have data to
        # render. Without this the discover output lives only in scan state
        # and the Overview / Applications pages stay empty.
        cluster_id = state.get("cluster_id", "default")
        apps = result.get("apps", [])
        for app in apps:
            app["id"] = f"{cluster_id}:{app.get('namespace', '')}:{app.get('name', '')}"
            app["cluster_id"] = cluster_id

        await self._persist_apps(cluster_id, apps)

        elapsed = time.monotonic() - start
        logger.info(
            "Discovery completed: %d apps in %d namespaces (%.1fs)",
            len(apps),
            len(result.get("namespaces", [])),
            elapsed,
        )

        return {
            "namespaces": result.get("namespaces", []),
            "apps": apps,
            "services": result.get("services", []),
            "topology": result.get("topology", {}),
            "agent_timings": {"discovery": elapsed},
        }

    async def _persist_apps(self, cluster_id: str, apps: list[dict[str, Any]]) -> None:
        """Upsert discovered apps into sre_apps so dashboard views see them."""
        if not self.db or not apps:
            return

        try:
            # Ensure the cluster row exists (FK requirement). The trigger
            # endpoint already creates it for manual scans, but cron scans
            # come through here without that guarantee.
            await self.db.pool.execute(
                """INSERT INTO sre_clusters (id, name, provider, region)
                   VALUES ($1, $2, 'gke', $3)
                   ON CONFLICT (id) DO NOTHING""",
                cluster_id,
                cluster_id,
                "asia-south1",
            )

            await self.db.pool.executemany(
                """INSERT INTO sre_apps (id, cluster_id, namespace, name, kind, is_active)
                   VALUES ($1, $2, $3, $4, $5, TRUE)
                   ON CONFLICT (id) DO UPDATE
                   SET namespace = EXCLUDED.namespace,
                       name = EXCLUDED.name,
                       kind = EXCLUDED.kind,
                       is_active = TRUE""",
                [
                    (
                        app["id"],
                        cluster_id,
                        app.get("namespace", ""),
                        app.get("name", ""),
                        app.get("kind", "Deployment"),
                    )
                    for app in apps
                ],
            )
            logger.info("Persisted %d apps to sre_apps for cluster=%s", len(apps), cluster_id)
        except Exception as e:
            logger.error("Failed to persist discovered apps: %s", e)

    @traceable_if_enabled(name="sre.monitor_parallel", run_type="chain")
    async def _node_monitor_parallel(self, state: SREState) -> dict[str, Any]:
        """Run all monitoring agents in parallel."""
        namespaces = state.get("namespaces", [])
        if not namespaces:
            return {
                "infra_findings": [],
                "log_findings": [],
                "perf_findings": [],
                "cost_findings": [],
                "capacity_findings": [],
            }

        from devai.sre.agents.capacity_planner import CapacityPlannerAgent
        from devai.sre.agents.cost_analyzer import CostAnalyzerAgent
        from devai.sre.agents.infra_monitor import InfraMonitorAgent
        from devai.sre.agents.log_analyzer import LogAnalyzerAgent
        from devai.sre.agents.perf_monitor import PerfMonitorAgent

        start = time.monotonic()

        # Run all 5 monitoring agents in parallel
        infra_agent = InfraMonitorAgent(self.config)
        log_agent = LogAnalyzerAgent(self.config)
        perf_agent = PerfMonitorAgent(self.config)
        cost_agent = CostAnalyzerAgent(self.config)
        capacity_agent = CapacityPlannerAgent(self.config)

        results = await asyncio.gather(
            infra_agent.run(namespaces),
            log_agent.run(namespaces),
            perf_agent.run(namespaces),
            cost_agent.run(namespaces),
            capacity_agent.run(namespaces),
            return_exceptions=True,
        )

        elapsed = time.monotonic() - start

        def safe_findings(result: Any, key: str = "findings") -> list:
            if isinstance(result, Exception):
                logger.error("Agent failed: %s", result)
                return []
            return result.get(key, []) if isinstance(result, dict) else []

        # Persist the cost analyzer report so the Cost Analysis dashboard
        # tab has data to render. The agent returns total monthly estimate +
        # potential savings + per-finding recommendations.
        cost_result = results[3] if not isinstance(results[3], Exception) else None
        if isinstance(cost_result, dict):
            await self._persist_cost_report(state.get("cluster_id", "default"), cost_result)

        timings = {**state.get("agent_timings", {}), "parallel_monitoring": elapsed}

        return {
            "infra_findings": safe_findings(results[0]),
            "log_findings": safe_findings(results[1]),
            "perf_findings": safe_findings(results[2]),
            "cost_findings": safe_findings(results[3]),
            "capacity_findings": safe_findings(results[4]),
            "agent_timings": timings,
        }

    async def _persist_cost_report(self, cluster_id: str, cost_data: dict[str, Any]) -> None:
        """Upsert today's cost report so the Cost Analysis tab has data."""
        if not self.db:
            return

        try:
            import json as _json

            monthly = float(cost_data.get("total_monthly_estimate_usd") or 0)
            savings = float(cost_data.get("potential_savings_usd") or 0)
            findings = cost_data.get("findings", []) or []

            # Daily cost = monthly / 30 (best-effort estimate from the LLM
            # analysis). The Overview page shows latest_daily_cost.
            daily = round(monthly / 30, 2) if monthly else 0.0

            recommendations = {
                "items": [
                    {
                        "title": f.get("title", ""),
                        "savings_usd": f.get("estimated_savings_usd", 0),
                        "recommendation": f.get("recommendation", ""),
                    }
                    for f in findings
                    if isinstance(f, dict)
                ],
                "potential_savings_usd": savings,
            }

            await self.db.pool.execute(
                """INSERT INTO sre_cost_reports
                   (cluster_id, report_date, provider, total_cost_usd,
                    breakdown, recommendations, monthly_forecast)
                   VALUES ($1, CURRENT_DATE, 'gcp', $2, $3, $4, $5)
                   ON CONFLICT (cluster_id, report_date, provider) DO UPDATE
                   SET total_cost_usd = EXCLUDED.total_cost_usd,
                       breakdown = EXCLUDED.breakdown,
                       recommendations = EXCLUDED.recommendations,
                       monthly_forecast = EXCLUDED.monthly_forecast""",
                cluster_id,
                daily,
                _json.dumps({"potential_savings_usd": savings}),
                _json.dumps(recommendations),
                monthly or None,
            )
            logger.info(
                "Persisted cost report: cluster=%s daily=$%.2f monthly=$%.2f findings=%d",
                cluster_id,
                daily,
                monthly,
                len(findings),
            )
        except Exception as e:
            logger.error("Failed to persist cost report: %s", e)

    @traceable_if_enabled(name="sre.correlate", run_type="chain")
    async def _node_correlate(self, state: SREState) -> dict[str, Any]:
        """Correlate, de-duplicate, and prioritize findings from all agents."""
        all_findings: list[dict[str, Any]] = []

        for source, findings in [
            ("infra_monitor", state.get("infra_findings", [])),
            ("log_analyzer", state.get("log_findings", [])),
            ("perf_monitor", state.get("perf_findings", [])),
            ("cost_analyzer", state.get("cost_findings", [])),
            ("capacity_planner", state.get("capacity_findings", [])),
        ]:
            for f in findings:
                f["detected_by"] = source
                all_findings.append(f)

        # De-duplicate by title similarity
        seen_titles: set[str] = set()
        unique_findings: list[dict[str, Any]] = []
        for f in all_findings:
            title = f.get("title", "").lower()[:50]
            if title not in seen_titles:
                seen_titles.add(title)
                unique_findings.append(f)

        # Sort by severity
        severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
        unique_findings.sort(key=lambda f: severity_order.get(f.get("severity", "info"), 5))

        logger.info("Correlated %d findings from %d raw", len(unique_findings), len(all_findings))

        # Write a per-app health snapshot for this scan so the
        # v_sre_app_reliability.health_pct_7d column has data to compute.
        await self._persist_health_snapshots(
            cluster_id=state.get("cluster_id", "default"),
            apps=state.get("apps", []),
            findings=unique_findings,
        )

        return {"all_findings": unique_findings}

    async def _persist_health_snapshots(
        self,
        cluster_id: str,
        apps: list[dict[str, Any]],
        findings: list[dict[str, Any]],
    ) -> None:
        """Write one sre_health_checks row per app for this scan.

        Each app gets a healthy/degraded/unhealthy classification based on
        the worst-severity finding that touches it. Apps with no findings
        are marked healthy. The 7-day rolling ratio of healthy vs total
        rows feeds ``v_sre_app_reliability.health_pct_7d``.
        """
        if not self.db or not apps:
            return

        import json as _json

        severity_rank = {"critical": 3, "high": 3, "medium": 2, "low": 1, "info": 0}
        app_severity: dict[str, int] = {}
        app_reasons: dict[str, str] = {}

        for finding in findings:
            matched = self._match_finding_to_app(finding, apps)
            if not matched:
                continue
            rank = severity_rank.get(finding.get("severity", "info"), 0)
            app_id = matched["id"]
            if rank > app_severity.get(app_id, -1):
                app_severity[app_id] = rank
                app_reasons[app_id] = finding.get("title", "") or finding.get("severity", "")

        rows: list[tuple] = []
        for app in apps:
            rank = app_severity.get(app["id"], 0)
            if rank >= 3:
                status = "unhealthy"
            elif rank == 2:
                status = "degraded"
            else:
                status = "healthy"

            details = {"reason": app_reasons[app["id"]]} if app["id"] in app_reasons else {}
            rows.append(
                (
                    cluster_id,
                    app["id"],
                    "scan_snapshot",
                    status,
                    _json.dumps(details),
                )
            )

        try:
            await self.db.pool.executemany(
                """INSERT INTO sre_health_checks
                   (cluster_id, app_id, check_type, status, details)
                   VALUES ($1, $2, $3, $4, $5)""",
                rows,
            )
            healthy = sum(1 for r in rows if r[3] == "healthy")
            logger.info(
                "Persisted %d health snapshots (%d healthy / %d issues) cluster=%s",
                len(rows),
                healthy,
                len(rows) - healthy,
                cluster_id,
            )
        except Exception as e:
            logger.error("Failed to persist health snapshots: %s", e)

    @staticmethod
    def _match_finding_to_app(
        finding: dict[str, Any], apps: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        """Best-effort match a finding back to a discovered app.

        Mirrors IncidentResponderAgent._find_app but without the
        all-else-fail fallback to apps[0] — for health snapshots we want
        unmatched findings to leave every app healthy rather than blaming
        a random app.
        """
        target = (finding.get("app") or finding.get("pod") or "").lower()
        namespace = finding.get("namespace", "")

        if target:
            for app in apps:
                name = (app.get("name") or "").lower()
                if name and (name in target or target in name):
                    return app

        if namespace:
            for app in apps:
                if app.get("namespace") == namespace:
                    return app

        return None

    @traceable_if_enabled(name="sre.respond", run_type="chain")
    async def _node_respond(self, state: SREState) -> dict[str, Any]:
        """Create issues/PRs for findings."""
        findings = state.get("all_findings", [])
        apps = state.get("apps", [])

        if not findings:
            return {"incidents_created": 0, "prs_created": 0}

        # Only respond to medium+ severity
        actionable = [f for f in findings if f.get("severity") in ("critical", "high", "medium")]
        if not actionable:
            return {"incidents_created": 0, "prs_created": 0}

        from devai.sre.agents.incident_responder import IncidentResponderAgent

        responder = IncidentResponderAgent(self.config, self.db)
        result = await responder.run(
            findings=actionable,
            apps=apps,
            cluster_id=state.get("cluster_id", "unknown"),
            scan_run_id=state.get("scan_id", ""),
        )

        return result

    @traceable_if_enabled(name="sre.learn", run_type="chain")
    async def _node_learn(self, state: SREState) -> dict[str, Any]:
        """Store learnings from this scan for future reference."""
        try:
            import redis.asyncio as redis_lib

            from devai.services.memory import AgentMemory

            r = redis_lib.from_url(self.config.redis_url, decode_responses=True)
            memory = AgentMemory(r)

            findings = state.get("all_findings", [])
            incidents = state.get("incidents_created", 0)

            await memory.remember(
                agent="sre_system",
                content=(
                    f"SRE scan completed: {len(findings)} findings, "
                    f"{incidents} incidents created. "
                    f"Namespaces: {', '.join(state.get('namespaces', []))}"
                ),
                memory_type="episodic",
                repo="cluster",
                tags=["sre_scan", f"findings:{len(findings)}"],
            )

            await r.aclose()
        except Exception as e:
            logger.debug("Memory storage failed: %s", e)

        return {}

    # --- Public API ---

    @traceable_if_enabled(name="sre_pipeline.run", run_type="chain")
    async def run(self, cluster_id: str = "default", trigger: str = "cron") -> SREState:
        """Run a full SRE monitoring scan."""
        from ulid import ULID

        initial_state: SREState = {
            "scan_id": str(ULID()),
            "cluster_id": cluster_id,
            "trigger": trigger,
            "namespaces": [],
            "apps": [],
            "all_findings": [],
            "incidents_created": 0,
            "prs_created": 0,
            "agent_timings": {},
            "error": None,
        }

        logger.info("SRE scan started: scan_id=%s cluster=%s trigger=%s", initial_state["scan_id"], cluster_id, trigger)

        try:
            final_state = await self._graph.ainvoke(initial_state)

            logger.info(
                "SRE scan completed: scan_id=%s findings=%d incidents=%d timings=%s",
                initial_state["scan_id"],
                len(final_state.get("all_findings", [])),
                final_state.get("incidents_created", 0),
                final_state.get("agent_timings", {}),
            )

            return final_state

        except Exception:
            logger.exception("SRE scan failed: scan_id=%s", initial_state["scan_id"])
            raise
