"""PostgreSQL database service for persistent ALM lifecycle storage.

Dual-store architecture:
  PostgreSQL — persistent, queryable, auditable (runs, agents, A2A, memory, audit)
  Redis      — ephemeral, fast (locks, sessions, live state, pub/sub)

Uses asyncpg for async PostgreSQL access.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


class SandboxQuotaExceeded(RuntimeError):
    """An atomic sandbox reservation crossed a tenant/user quota."""


class Database:
    """Async PostgreSQL client for the DevAI ALM lifecycle store."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool = None

    async def connect(self) -> None:
        """Create the connection pool with retry + lazy init.

        Previously this opened ``min_size=2`` connections eagerly at
        startup. In the production mesh that's flaky — one of the two
        initial connections occasionally gets reset mid-handshake by
        ambient ztunnel under load, and the pool init then fails the
        whole process with ConnectionDoesNotExistError. devai-sre
        would CrashLoopBackOff while devai-api survived only because
        its lifespan happened to swallow the exception.

        Fix: ``min_size=0`` (lazy — connections dial on first
        ``pool.acquire()``) plus an exponential-backoff retry. Both
        services now start cleanly even when the first one or two SYN
        attempts get reset.
        """
        import asyncio

        import asyncpg

        attempts = 5
        delay = 1.0
        last: Exception | None = None
        for i in range(attempts):
            try:
                self._pool = await asyncpg.create_pool(
                    self._dsn,
                    min_size=0,
                    max_size=10,
                    timeout=10.0,
                    command_timeout=30.0,
                    # Ambient ztunnel resets idle TCP connections, leaving the
                    # pool holding dead sockets that raise
                    # ConnectionDoesNotExistError on next use. Recycle idle
                    # connections well before that window so the pool rarely
                    # hands out a reset connection. Paired with per-query
                    # reconnect retries in the call sites.
                    max_inactive_connection_lifetime=30.0,
                )
                logger.info(
                    "PostgreSQL pool ready: %s (attempt %d/%d)",
                    self._dsn.split("@")[-1] if "@" in self._dsn else "local",
                    i + 1,
                    attempts,
                )
                return
            except (
                asyncpg.exceptions.ConnectionDoesNotExistError,
                asyncpg.exceptions.PostgresError,
                ConnectionResetError,
                OSError,
            ) as e:
                last = e
                logger.warning(
                    "PostgreSQL pool init attempt %d/%d failed: %s — retrying in %.1fs",
                    i + 1,
                    attempts,
                    e,
                    delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, 8.0)
        raise RuntimeError(f"PostgreSQL pool init failed after {attempts} attempts: {last}")

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()

    @property
    def pool(self):
        if not self._pool:
            raise RuntimeError("Database not connected. Call connect() first.")
        return self._pool

    # =========================================================================
    # Pipeline Runs
    # =========================================================================

    async def create_run(
        self,
        run_id: str,
        repo: str,
        trigger_type: str,
        trigger_ref: str,
        requirements: str,
        governance: str = "",
    ) -> None:
        await self.pool.execute(
            """INSERT INTO pipeline_runs
               (id, repo_full_name, trigger_type, trigger_ref, requirements, governance_snapshot)
               VALUES ($1, $2, $3, $4, $5, $6)""",
            run_id,
            repo,
            trigger_type,
            trigger_ref,
            requirements,
            governance,
        )

    async def update_run_stage(self, run_id: str, stage: str) -> None:
        await self.pool.execute(
            "UPDATE pipeline_runs SET stage = $1, updated_at = NOW() WHERE id = $2",
            stage,
            run_id,
        )

    async def complete_run(self, run_id: str, stage: str) -> None:
        await self.pool.execute(
            "UPDATE pipeline_runs SET stage = $1, completed_at = NOW(), updated_at = NOW() WHERE id = $2",
            stage,
            run_id,
        )

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = await self.pool.fetchrow("SELECT * FROM pipeline_runs WHERE id = $1", run_id)
        return dict(row) if row else None

    async def list_runs(self, repo: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        if repo:
            rows = await self.pool.fetch(
                "SELECT * FROM pipeline_runs WHERE repo_full_name = $1 ORDER BY created_at DESC LIMIT $2",
                repo,
                limit,
            )
        else:
            rows = await self.pool.fetch(
                "SELECT * FROM pipeline_runs ORDER BY created_at DESC LIMIT $1",
                limit,
            )
        return [dict(r) for r in rows]

    # =========================================================================
    # Agent Executions
    # =========================================================================

    async def record_llm_call(
        self,
        *,
        run_id: str,
        agent_name: str,
        provider: str,
        model: str,
        tokens_input: int,
        tokens_output: int,
        cost_usd: float,
        duration_ms: float,
        status: str = "ok",
        tenant_id: str = "",
        user_id: str = "",
        triggered_by: str = "",
    ) -> None:
        """Persist one LLM call as a completed agent_executions row.

        This is what feeds the analytics rollups (top agents by cost,
        cost-by-model, cost timeseries, the agent table). One row per
        call — the SUM/AVG aggregations stay correct, and it works for
        EVERY pipeline (the blueprint executor never calls the legacy
        record_agent_start/complete pair). Best-effort by contract.
        """
        try:
            await self.pool.execute(
                """INSERT INTO agent_executions
                   (run_id, agent_name, status, started_at, completed_at, duration_ms,
                    provider, model, tokens_input, tokens_output, llm_cost_usd,
                    tenant_id, user_id, triggered_by)
                   VALUES ($1, $2, $3, NOW(), NOW(), $4, $5, $6, $7, $8, $9, $10, $11, $12)""",
                run_id,
                agent_name or "unknown",
                "completed" if status == "ok" else "failed",
                int(duration_ms),
                provider,
                model,
                int(tokens_input),
                int(tokens_output),
                float(cost_usd),
                tenant_id,
                user_id,
                triggered_by,
            )
        except Exception:  # noqa: BLE001 — accounting must never break an LLM call
            logger.debug("record_llm_call failed (agent=%s)", agent_name, exc_info=True)

    async def record_agent_start(self, run_id: str, agent_name: str, provider: str, model: str) -> str:
        row = await self.pool.fetchrow(
            """INSERT INTO agent_executions (run_id, agent_name, status, started_at, provider, model)
               VALUES ($1, $2, 'running', NOW(), $3, $4) RETURNING id""",
            run_id,
            agent_name,
            provider,
            model,
        )
        return str(row["id"])

    async def record_agent_complete(
        self,
        exec_id: str,
        status: str,
        output_summary: str,
        output_data: dict,
        tokens_input: int = 0,
        tokens_output: int = 0,
        cost_usd: float = 0.0,
        error: str | None = None,
        provider: str = "",
        model: str = "",
    ) -> None:
        # Compute USD from the rate card when the caller didn't supply a cost
        # but we have tokens + a model — this is what turns the analytics cost
        # views from $0 into real money.
        if cost_usd <= 0 and (tokens_input or tokens_output) and model:
            try:
                from devai.analytics.pricing import estimate_cost

                cost_usd = estimate_cost(provider, model, tokens_input, tokens_output)
            except Exception:  # noqa: BLE001
                logger.debug("cost estimate failed for %s/%s", provider, model, exc_info=True)
        await self.pool.execute(
            """UPDATE agent_executions SET
               status = $1, completed_at = NOW(),
               duration_ms = EXTRACT(EPOCH FROM (NOW() - started_at)) * 1000,
               output_summary = $2, output_data = $3,
               tokens_input = $4, tokens_output = $5, llm_cost_usd = $6,
               error_message = $7
               WHERE id = $8""",
            status,
            output_summary,
            json.dumps(output_data),
            tokens_input,
            tokens_output,
            cost_usd,
            error,
            exec_id,
        )

    async def get_agent_executions(self, run_id: str) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            "SELECT * FROM agent_executions WHERE run_id = $1 ORDER BY started_at",
            run_id,
        )
        return [dict(r) for r in rows]

    # =========================================================================
    # A2A Messages
    # =========================================================================

    async def store_a2a_message(
        self,
        msg_id: str,
        run_id: str,
        from_agent: str,
        to_agent: str,
        message_type: str,
        subject: str,
        body: str,
        payload: dict | None = None,
        in_reply_to: str | None = None,
    ) -> None:
        await self.pool.execute(
            """INSERT INTO a2a_messages
               (id, run_id, from_agent, to_agent, message_type, subject, body, payload, in_reply_to)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)""",
            msg_id,
            run_id,
            from_agent,
            to_agent,
            message_type,
            subject,
            body,
            json.dumps(payload or {}),
            in_reply_to,
        )

    async def get_a2a_messages(self, run_id: str) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            "SELECT * FROM a2a_messages WHERE run_id = $1 ORDER BY created_at",
            run_id,
        )
        return [dict(r) for r in rows]

    # =========================================================================
    # Audit Log (append-only)
    # =========================================================================

    async def audit(
        self,
        action: str,
        actor: str,
        actor_type: str = "agent",
        run_id: str | None = None,
        agent_name: str | None = None,
        entity_type: str | None = None,
        entity_ref: str | None = None,
        details: dict | None = None,
    ) -> None:
        await self.pool.execute(
            """INSERT INTO audit_log
               (run_id, agent_name, action, entity_type, entity_ref, details, actor, actor_type)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8)""",
            run_id,
            agent_name,
            action,
            entity_type,
            entity_ref,
            json.dumps(details or {}),
            actor,
            actor_type,
        )

    async def get_audit_log(
        self,
        run_id: str | None = None,
        agent_name: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if run_id:
            rows = await self.pool.fetch(
                "SELECT * FROM audit_log WHERE run_id = $1 ORDER BY created_at DESC LIMIT $2",
                run_id,
                limit,
            )
        elif agent_name:
            rows = await self.pool.fetch(
                "SELECT * FROM audit_log WHERE agent_name = $1 ORDER BY created_at DESC LIMIT $2",
                agent_name,
                limit,
            )
        else:
            rows = await self.pool.fetch(
                "SELECT * FROM audit_log ORDER BY created_at DESC LIMIT $1",
                limit,
            )
        return [dict(r) for r in rows]

    # =========================================================================
    # Agent Memory (PostgreSQL + pgvector)
    # =========================================================================

    async def store_memory(
        self,
        memory_id: str,
        agent: str,
        repo: str,
        memory_type: str,
        content: str,
        tags: list[str] | None = None,
        metadata: dict | None = None,
        embedding: list[float] | None = None,
        expires_at: datetime | None = None,
    ) -> None:
        await self.pool.execute(
            """INSERT INTO agent_memories
               (id, agent, repo, memory_type, content, tags, metadata, embedding, expires_at)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
               ON CONFLICT (id) DO UPDATE SET
               content = EXCLUDED.content, tags = EXCLUDED.tags,
               metadata = EXCLUDED.metadata, embedding = EXCLUDED.embedding,
               updated_at = NOW()""",
            memory_id,
            agent,
            repo,
            memory_type,
            content,
            tags or [],
            json.dumps(metadata or {}),
            embedding,
            expires_at,
        )

    async def recall_memories(
        self,
        agent: str | None = None,
        repo: str | None = None,
        memory_type: str | None = None,
        tags: list[str] | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        conditions = ["is_active = TRUE"]
        params: list[Any] = []
        idx = 1

        if agent:
            conditions.append(f"agent = ${idx}")
            params.append(agent)
            idx += 1
        if repo:
            conditions.append(f"(repo = ${idx} OR repo = 'global')")
            params.append(repo)
            idx += 1
        if memory_type:
            conditions.append(f"memory_type = ${idx}")
            params.append(memory_type)
            idx += 1
        if tags:
            conditions.append(f"tags && ${idx}")
            params.append(tags)
            idx += 1

        where = " AND ".join(conditions)
        params.append(limit)

        rows = await self.pool.fetch(
            f"""SELECT * FROM agent_memories
                WHERE {where}
                ORDER BY relevance_score DESC, created_at DESC
                LIMIT ${idx}""",
            *params,
        )

        # Bump access counts
        ids = [r["id"] for r in rows]
        if ids:
            await self.pool.execute(
                "UPDATE agent_memories SET access_count = access_count + 1, last_accessed = NOW() WHERE id = ANY($1)",
                ids,
            )

        return [dict(r) for r in rows]

    async def semantic_search(
        self,
        embedding: list[float],
        repo: str | None = None,
        limit: int = 5,
        agent: str | None = None,
        memory_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """Find memories by semantic similarity using pgvector.

        All filters are applied in SQL so the caller always gets up to
        `limit` matching rows — post-filtering in Python silently shrinks
        the result set below the requested k.
        """
        clauses = ["is_active = TRUE", "embedding IS NOT NULL"]
        params: list[Any] = [embedding]
        if repo:
            params.append(repo)
            clauses.append(f"(repo = ${len(params)} OR repo = 'global')")
        if agent:
            params.append(agent)
            clauses.append(f"agent = ${len(params)}")
        if memory_type:
            params.append(memory_type)
            clauses.append(f"memory_type = ${len(params)}")
        params.append(limit)
        rows = await self.pool.fetch(
            f"""SELECT *, 1 - (embedding <=> $1::vector) AS similarity
                FROM agent_memories
                WHERE {" AND ".join(clauses)}
                ORDER BY embedding <=> $1::vector
                LIMIT ${len(params)}""",
            *params,
        )
        return [dict(r) for r in rows]

    # =========================================================================
    # Security Findings
    # =========================================================================

    async def store_security_finding(
        self,
        run_id: str,
        repo: str,
        scanner: str,
        severity: str,
        issue: str,
        pr_number: int | None = None,
        file_path: str | None = None,
        line_number: int | None = None,
        tool: str | None = None,
        cve_id: str | None = None,
        cwe_id: str | None = None,
    ) -> None:
        await self.pool.execute(
            """INSERT INTO security_findings
               (run_id, repo, scanner, severity, issue, pr_number, file_path, line_number, tool, cve_id, cwe_id)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)""",
            run_id,
            repo,
            scanner,
            severity,
            issue,
            pr_number,
            file_path,
            line_number,
            tool,
            cve_id,
            cwe_id,
        )

    # =========================================================================
    # DB Migration Audit
    # =========================================================================

    async def record_migration(
        self,
        run_id: str,
        repo: str,
        migration_type: str,
        migration_file: str,
        migration_hash: str | None = None,
        is_destructive: bool = False,
        tables_created: list[str] | None = None,
        tables_modified: list[str] | None = None,
        columns_added: list[str] | None = None,
    ) -> None:
        await self.pool.execute(
            """INSERT INTO db_migration_audit
               (run_id, repo, migration_type, migration_file, migration_hash,
                is_destructive, tables_created, tables_modified, columns_added)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)""",
            run_id,
            repo,
            migration_type,
            migration_file,
            migration_hash,
            is_destructive,
            tables_created or [],
            tables_modified or [],
            columns_added or [],
        )

    # =========================================================================
    # Approval Gates
    # =========================================================================

    async def create_gate(self, run_id: str, gate_name: str, agent_name: str) -> str:
        row = await self.pool.fetchrow(
            """INSERT INTO approval_gates (run_id, gate_name, agent_name)
               VALUES ($1, $2, $3) RETURNING id""",
            run_id,
            gate_name,
            agent_name,
        )
        return str(row["id"])

    async def decide_gate(self, run_id: str, gate_name: str, decision: str, decided_by: str, reason: str = "") -> None:
        await self.pool.execute(
            """UPDATE approval_gates SET status = $1, decided_by = $2, decided_at = NOW(), reason = $3
               WHERE run_id = $4 AND gate_name = $5 AND status = 'pending'""",
            decision,
            decided_by,
            reason,
            run_id,
            gate_name,
        )
        # Audit the decision
        await self.audit(
            action=f"gate_{decision}",
            actor=decided_by,
            actor_type="user",
            run_id=run_id,
            entity_type="gate",
            entity_ref=gate_name,
        )

    # =========================================================================
    # Dashboard Queries
    # =========================================================================

    async def get_recent_activity(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            "SELECT * FROM v_recent_activity LIMIT $1",
            limit,
        )
        return [dict(r) for r in rows]

    async def get_agent_stats(self) -> list[dict[str, Any]]:
        rows = await self.pool.fetch("SELECT * FROM v_agent_stats")
        return [dict(r) for r in rows]

    async def get_security_posture(self, repo: str | None = None) -> list[dict[str, Any]]:
        if repo:
            rows = await self.pool.fetch(
                "SELECT * FROM v_security_posture WHERE repo = $1",
                repo,
            )
        else:
            rows = await self.pool.fetch("SELECT * FROM v_security_posture")
        return [dict(r) for r in rows]

    # =========================================================================
    # Analytics — read-only rollups over agent_executions + SRE views.
    #
    # Backs the dashboard /analytics page. Run-level stats come from the live
    # runtime (Redis); these methods cover the per-agent / per-LLM token + cost
    # rollups (agent_executions) and a best-effort SRE summary. Every method is
    # best-effort: callers wrap in try/except so a missing table → empty.
    # =========================================================================

    async def analytics_agent_stats(
        self, days: int = 30, *, tenant_id: str = "", user_id: str = ""
    ) -> list[dict[str, Any]]:
        """Per-agent executions, avg duration, tokens, cost, failures."""
        rows = await self.pool.fetch(
            """SELECT agent_name,
                      COUNT(*)::int                                   AS executions,
                      AVG(duration_ms)                                AS avg_duration_ms,
                      COALESCE(SUM(tokens_input), 0)::bigint          AS tokens_input,
                      COALESCE(SUM(tokens_output), 0)::bigint         AS tokens_output,
                      COALESCE(SUM(llm_cost_usd), 0)::float           AS total_cost_usd,
                      SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END)::int AS failures
               FROM agent_executions
               WHERE started_at >= NOW() - make_interval(days => $1)
                 AND ($2 = '' OR tenant_id = $2)
                 AND ($3 = '' OR user_id = $3)
               GROUP BY agent_name
               ORDER BY executions DESC""",
            days,
            tenant_id,
            user_id,
        )
        return [dict(r) for r in rows]

    async def analytics_llm_cost_by_model(
        self, days: int = 30, *, tenant_id: str = "", user_id: str = ""
    ) -> list[dict[str, Any]]:
        """Calls, tokens, and USD cost grouped by provider/model."""
        rows = await self.pool.fetch(
            """SELECT COALESCE(provider, 'unknown')          AS provider,
                      COALESCE(model, 'unknown')             AS model,
                      COUNT(*)::int                          AS calls,
                      COALESCE(SUM(tokens_input), 0)::bigint  AS tokens_input,
                      COALESCE(SUM(tokens_output), 0)::bigint AS tokens_output,
                      COALESCE(SUM(llm_cost_usd), 0)::float   AS cost_usd
               FROM agent_executions
               WHERE started_at >= NOW() - make_interval(days => $1)
                 AND ($2 = '' OR tenant_id = $2)
                 AND ($3 = '' OR user_id = $3)
               GROUP BY provider, model
               ORDER BY cost_usd DESC NULLS LAST""",
            days,
            tenant_id,
            user_id,
        )
        return [dict(r) for r in rows]

    async def analytics_llm_cost_timeseries(
        self, days: int = 30, *, tenant_id: str = "", user_id: str = ""
    ) -> list[dict[str, Any]]:
        """Daily LLM cost + token totals."""
        rows = await self.pool.fetch(
            """SELECT to_char(date_trunc('day', started_at), 'YYYY-MM-DD') AS date,
                      COALESCE(SUM(llm_cost_usd), 0)::float                  AS cost_usd,
                      COALESCE(SUM(tokens_input + tokens_output), 0)::bigint AS tokens
               FROM agent_executions
               WHERE started_at >= NOW() - make_interval(days => $1)
                 AND ($2 = '' OR tenant_id = $2)
                 AND ($3 = '' OR user_id = $3)
               GROUP BY 1 ORDER BY 1""",
            days,
            tenant_id,
            user_id,
        )
        return [dict(r) for r in rows]

    # ── Evals ─────────────────────────────────────────────────────────

    async def record_eval(
        self,
        *,
        run_id: str,
        evaluator: str,
        score: float,
        passed: bool,
        stage: str = "",
        agent_name: str = "",
        triggered_by: str = "",
        tenant_id: str = "",
        user_id: str = "",
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Persist one quality eval (review/security/tests → 0..1 score).

        Best-effort: a missing table (pre-bootstrap) or any failure is
        swallowed so capturing an eval never breaks a run.
        """
        try:
            scoped_detail = dict(detail or {})
            if tenant_id:
                scoped_detail["tenant_id"] = tenant_id
            if user_id:
                scoped_detail["user_id"] = user_id
            await self.pool.execute(
                """INSERT INTO agent_evals
                   (run_id, stage, agent_name, evaluator, score, passed, triggered_by, detail)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8)""",
                run_id,
                stage,
                agent_name,
                evaluator,
                float(max(0.0, min(1.0, score))),
                bool(passed),
                triggered_by,
                json.dumps(scoped_detail),
            )
        except Exception:  # noqa: BLE001
            logger.debug("record_eval failed (evaluator=%s)", evaluator, exc_info=True)

    async def analytics_evals(
        self,
        days: int = 30,
        *,
        tenant_id: str = "",
        user_id: str = "",
    ) -> dict[str, Any]:
        """Eval summary scoped to a tenant and, for non-admins, one subject."""
        try:
            where = "created_at >= NOW() - make_interval(days => $1)"
            args: list[Any] = [days]
            if tenant_id:
                args.append(tenant_id)
                where += f" AND detail->>'tenant_id' = ${len(args)}"
            if user_id:
                args.append(user_id)
                where += f" AND detail->>'user_id' = ${len(args)}"
            summary = await self.pool.fetchrow(
                f"""SELECT COUNT(*)::int AS evals,
                           COALESCE(AVG(score), 0)::float AS avg_score,
                           COALESCE(AVG(CASE WHEN passed THEN 1.0 ELSE 0.0 END), 0)::float AS pass_rate
                    FROM agent_evals WHERE {where}""",
                *args,
            )
            by_eval = await self.pool.fetch(
                f"""SELECT evaluator,
                           COUNT(*)::int AS evals,
                           COALESCE(AVG(score), 0)::float AS avg_score,
                           COALESCE(AVG(CASE WHEN passed THEN 1.0 ELSE 0.0 END), 0)::float AS pass_rate
                    FROM agent_evals WHERE {where}
                    GROUP BY evaluator ORDER BY evals DESC""",
                *args,
            )
            recent = await self.pool.fetch(
                f"""SELECT run_id, stage, evaluator, score, passed,
                           to_char(created_at, 'YYYY-MM-DD\"T\"HH24:MI:SSZ') AS created_at
                    FROM agent_evals WHERE {where}
                    ORDER BY created_at DESC LIMIT 50""",
                *args,
            )
            return {
                "summary": dict(summary) if summary else {"evals": 0, "avg_score": 0, "pass_rate": 0},
                "by_evaluator": [dict(r) for r in by_eval],
                "recent": [dict(r) for r in recent],
            }
        except Exception:  # noqa: BLE001
            logger.debug("analytics_evals query failed", exc_info=True)
            return {"summary": {"evals": 0, "avg_score": 0, "pass_rate": 0}, "by_evaluator": [], "recent": []}

    async def analytics_memory_summary(self) -> dict[str, Any]:
        """Corpus-level memory stats from agent_memories (pgvector store)."""
        row = await self.pool.fetchrow(
            """SELECT
                 COUNT(*)::int                                                    AS total,
                 COUNT(embedding)::int                                            AS embedded,
                 COUNT(*) FILTER (WHERE memory_type = 'episodic')::int            AS episodic,
                 COUNT(*) FILTER (WHERE memory_type = 'semantic')::int            AS semantic,
                 COUNT(*) FILTER (WHERE memory_type = 'procedural')::int          AS procedural,
                 COUNT(*) FILTER (WHERE created_at >= NOW() - interval '24 hours')::int AS last_24h,
                 COUNT(*) FILTER (WHERE created_at >= NOW() - interval '7 days')::int   AS last_7d,
                 COALESCE(AVG(relevance_score), 0)::float                         AS avg_relevance,
                 COALESCE(SUM(access_count), 0)::bigint                           AS total_recalls
               FROM agent_memories
               WHERE is_active = TRUE"""
        )
        return dict(row) if row else {}

    async def analytics_memory_by_agent(self, limit: int = 12) -> list[dict[str, Any]]:
        """Memory counts + recall activity per writing agent."""
        rows = await self.pool.fetch(
            """SELECT agent,
                      COUNT(*)::int                          AS memories,
                      COUNT(embedding)::int                  AS embedded,
                      COALESCE(SUM(access_count), 0)::bigint AS recalls,
                      to_char(MAX(created_at), 'YYYY-MM-DD"T"HH24:MI:SSZ') AS last_written_at
               FROM agent_memories
               WHERE is_active = TRUE
               GROUP BY agent ORDER BY memories DESC LIMIT $1""",
            limit,
        )
        return [dict(r) for r in rows]

    async def analytics_memory_by_repo(self, limit: int = 12) -> list[dict[str, Any]]:
        """Memory counts per repo scope."""
        rows = await self.pool.fetch(
            """SELECT repo,
                      COUNT(*)::int         AS memories,
                      COUNT(embedding)::int AS embedded
               FROM agent_memories
               WHERE is_active = TRUE
               GROUP BY repo ORDER BY memories DESC LIMIT $1""",
            limit,
        )
        return [dict(r) for r in rows]

    async def analytics_memory_timeseries(self, days: int = 30) -> list[dict[str, Any]]:
        """Memories written per day, split by type."""
        rows = await self.pool.fetch(
            """SELECT to_char(date_trunc('day', created_at), 'YYYY-MM-DD') AS date,
                      COUNT(*)::int                                            AS total,
                      COUNT(*) FILTER (WHERE memory_type = 'episodic')::int    AS episodic,
                      COUNT(*) FILTER (WHERE memory_type = 'semantic')::int    AS semantic,
                      COUNT(*) FILTER (WHERE memory_type = 'procedural')::int  AS procedural
               FROM agent_memories
               WHERE is_active = TRUE
                 AND created_at >= NOW() - make_interval(days => $1)
               GROUP BY 1 ORDER BY 1""",
            days,
        )
        return [dict(r) for r in rows]

    async def analytics_sre_summary(self) -> dict[str, Any]:
        """Best-effort SRE counts from the shared devai_db SRE tables."""
        row = await self.pool.fetchrow(
            """SELECT
                 (SELECT COUNT(*) FROM sre_incidents WHERE status = 'open')::int AS open_incidents,
                 (SELECT COUNT(*) FROM sre_incidents
                    WHERE status = 'open' AND severity = 'critical')::int        AS critical_incidents,
                 (SELECT COUNT(*) FROM sre_apps)::int                            AS total_apps,
                 (SELECT COALESCE(SUM(total_cost_usd), 0)::float FROM sre_cost_reports
                    WHERE report_date >= NOW() - make_interval(days => 1))       AS latest_daily_cost"""
        )
        return dict(row) if row else {}

    async def analytics_incident_slo(self, days: int = 30) -> dict[str, Any]:
        """Incident-side SLO inputs: MTTR, counts, resolution rate."""
        row = await self.pool.fetchrow(
            """SELECT
                 COUNT(*)::int                                                   AS total_incidents,
                 SUM(CASE WHEN severity = 'critical' THEN 1 ELSE 0 END)::int     AS critical_incidents,
                 SUM(CASE WHEN status = 'resolved' THEN 1 ELSE 0 END)::int       AS resolved_incidents,
                 AVG(mttr_seconds) FILTER (WHERE mttr_seconds IS NOT NULL)       AS avg_mttr_seconds,
                 MAX(mttr_seconds)                                               AS worst_mttr_seconds
               FROM sre_incidents
               WHERE created_at >= NOW() - make_interval(days => $1)""",
            days,
        )
        return dict(row) if row else {}

    async def analytics_app_reliability(self) -> list[dict[str, Any]]:
        """Per-app reliability rollup (uptime %, MTTR) from the SRE view."""
        rows = await self.pool.fetch("SELECT * FROM v_sre_app_reliability")
        return [dict(r) for r in rows]

    # =========================================================================
    # SRE Studio — config drafts (author → dry-run → publish)
    #
    # DDL lives in tesserix-k8s db-schema-bootstrap (sre_config_drafts).
    # A draft is the unpublished form of a user-authored SRE artifact:
    #   kind ∈ {blueprint, agent}, the validated YAML, a status
    #   (draft | published), and the last dry-run summary (JSONB).
    # =========================================================================

    async def create_draft(
        self,
        draft_id: str,
        kind: str,
        name: str,
        yaml_text: str,
        created_by: str,
        description: str = "",
    ) -> None:
        await self.pool.execute(
            """INSERT INTO sre_config_drafts
                 (id, kind, name, yaml, description, status, created_by)
               VALUES ($1, $2, $3, $4, $5, 'draft', $6)""",
            draft_id,
            kind,
            name,
            yaml_text,
            description,
            created_by,
        )

    async def get_draft(self, draft_id: str) -> dict[str, Any] | None:
        row = await self.pool.fetchrow("SELECT * FROM sre_config_drafts WHERE id = $1", draft_id)
        return dict(row) if row else None

    async def list_drafts(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if status:
            rows = await self.pool.fetch(
                "SELECT * FROM sre_config_drafts WHERE status = $1 ORDER BY updated_at DESC LIMIT $2",
                status,
                limit,
            )
        else:
            rows = await self.pool.fetch(
                "SELECT * FROM sre_config_drafts ORDER BY updated_at DESC LIMIT $1",
                limit,
            )
        return [dict(r) for r in rows]

    async def update_draft(
        self,
        draft_id: str,
        *,
        yaml_text: str | None = None,
        name: str | None = None,
        description: str | None = None,
    ) -> None:
        await self.pool.execute(
            """UPDATE sre_config_drafts
                 SET yaml        = COALESCE($2, yaml),
                     name        = COALESCE($3, name),
                     description = COALESCE($4, description),
                     updated_at  = NOW()
               WHERE id = $1""",
            draft_id,
            yaml_text,
            name,
            description,
        )

    async def set_draft_dry_run(self, draft_id: str, summary: str) -> None:
        await self.pool.execute(
            "UPDATE sre_config_drafts SET dry_run_summary = $2::jsonb, last_dry_run_at = NOW(), updated_at = NOW() "
            "WHERE id = $1",
            draft_id,
            summary,
        )

    async def set_draft_status(self, draft_id: str, status: str) -> None:
        await self.pool.execute(
            "UPDATE sre_config_drafts SET status = $2, "
            "published_at = CASE WHEN $2 = 'published' THEN NOW() ELSE published_at END, "
            "updated_at = NOW() WHERE id = $1",
            draft_id,
            status,
        )

    async def delete_draft(self, draft_id: str) -> bool:
        result = await self.pool.execute("DELETE FROM sre_config_drafts WHERE id = $1", draft_id)
        return result.split()[-1] != "0"

    # =========================================================================
    # SRE Studio — schedules (cadence for published blueprints)
    #
    # DDL lives in tesserix-k8s db-schema-bootstrap (sre_schedules).
    # =========================================================================

    async def create_schedule(
        self,
        schedule_id: str,
        blueprint: str,
        cron: str,
        cluster_id: str,
        created_by: str,
        enabled: bool = True,
    ) -> None:
        await self.pool.execute(
            """INSERT INTO sre_schedules
                 (id, blueprint, cron, cluster_id, enabled, created_by)
               VALUES ($1, $2, $3, $4, $5, $6)""",
            schedule_id,
            blueprint,
            cron,
            cluster_id,
            enabled,
            created_by,
        )

    async def list_schedules(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        if enabled_only:
            rows = await self.pool.fetch("SELECT * FROM sre_schedules WHERE enabled = true ORDER BY created_at DESC")
        else:
            rows = await self.pool.fetch("SELECT * FROM sre_schedules ORDER BY created_at DESC")
        return [dict(r) for r in rows]

    async def update_schedule(
        self,
        schedule_id: str,
        *,
        cron: str | None = None,
        enabled: bool | None = None,
    ) -> None:
        await self.pool.execute(
            """UPDATE sre_schedules
                 SET cron    = COALESCE($2, cron),
                     enabled = COALESCE($3, enabled),
                     updated_at = NOW()
               WHERE id = $1""",
            schedule_id,
            cron,
            enabled,
        )

    async def mark_schedule_ran(self, schedule_id: str) -> None:
        await self.pool.execute(
            "UPDATE sre_schedules SET last_run_at = NOW() WHERE id = $1",
            schedule_id,
        )

    async def delete_schedule(self, schedule_id: str) -> bool:
        result = await self.pool.execute("DELETE FROM sre_schedules WHERE id = $1", schedule_id)
        return result.split()[-1] != "0"

    # =========================================================================
    # Live previews — per-task ephemeral preview environments
    #
    # DDL lives in tesserix-k8s db-schema-bootstrap (preview_sessions). One row
    # per running preview; the runtime resources (PVC/Deployment/Service/VS)
    # live in the devai-previews namespace and are named after `deployment`.
    # =========================================================================

    async def create_preview_session(
        self,
        session_id: str,
        repo: str,
        ref: str,
        owner: str,
        fe_url: str,
        deployment: str,
        status: str = "starting",
    ) -> None:
        await self.pool.execute(
            """INSERT INTO preview_sessions
                 (id, repo, ref, owner, fe_url, deployment, status)
               VALUES ($1, $2, $3, $4, $5, $6, $7)""",
            session_id,
            repo,
            ref,
            owner,
            fe_url,
            deployment,
            status,
        )

    async def get_preview_session(self, session_id: str) -> dict[str, Any] | None:
        row = await self.pool.fetchrow("SELECT * FROM preview_sessions WHERE id = $1", session_id)
        return dict(row) if row else None

    async def list_preview_sessions(self, owner: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if owner:
            rows = await self.pool.fetch(
                "SELECT * FROM preview_sessions WHERE owner = $1 ORDER BY created_at DESC LIMIT $2",
                owner,
                limit,
            )
        else:
            rows = await self.pool.fetch("SELECT * FROM preview_sessions ORDER BY created_at DESC LIMIT $1", limit)
        return [dict(r) for r in rows]

    async def find_live_preview(self, repo: str, ref: str, owner: str) -> dict[str, Any] | None:
        """Reuse an existing non-stopped preview for the same (repo, ref, owner)."""
        row = await self.pool.fetchrow(
            "SELECT * FROM preview_sessions WHERE repo = $1 AND ref = $2 AND owner = $3 "
            "AND status <> 'stopped' ORDER BY created_at DESC LIMIT 1",
            repo,
            ref,
            owner,
        )
        return dict(row) if row else None

    async def touch_preview_session(self, session_id: str) -> None:
        await self.pool.execute("UPDATE preview_sessions SET last_access_at = NOW() WHERE id = $1", session_id)

    async def set_preview_session_status(self, session_id: str, status: str) -> None:
        await self.pool.execute(
            "UPDATE preview_sessions SET status = $2, updated_at = NOW() WHERE id = $1",
            session_id,
            status,
        )

    # =========================================================================
    # Evaluation datasets and suites — immutable, user-scoped versions (#184)
    # =========================================================================

    async def create_eval_dataset_version(
        self,
        *,
        owner_scope: str,
        tenant_id: str,
        user_id: str,
        name: str,
        version: str,
        description: str,
        case_count: int,
        content_hash: str,
        blob_key: str,
    ) -> dict[str, Any] | None:
        async with self.pool.acquire() as connection, connection.transaction():
            dataset = await connection.fetchrow(
                """INSERT INTO eval_datasets
                         (owner_scope, tenant_id, user_id, name, description)
                       VALUES ($1, $2, $3, $4, $5)
                       ON CONFLICT (owner_scope, name)
                       DO UPDATE SET updated_at = eval_datasets.updated_at
                       RETURNING id""",
                owner_scope,
                tenant_id,
                user_id,
                name,
                description,
            )
            if dataset is None:
                return None
            row = await connection.fetchrow(
                """WITH inserted AS (
                         INSERT INTO eval_dataset_versions
                             (dataset_id, version, description, case_count, content_hash, blob_key)
                         VALUES ($1, $2, $3, $4, $5, $6)
                         ON CONFLICT (dataset_id, version) DO NOTHING
                         RETURNING *
                       )
                       SELECT d.name, i.version, i.description, i.case_count,
                              i.content_hash, i.blob_key, d.owner_scope, i.created_at
                         FROM inserted i
                         JOIN eval_datasets d ON d.id = i.dataset_id""",
                dataset["id"],
                version,
                description,
                case_count,
                content_hash,
                blob_key,
            )
        return dict(row) if row else None

    async def list_eval_datasets(self, owner_scope: str, *, limit: int = 100) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            """SELECT d.name, v.version, v.description, v.case_count,
                      v.content_hash, v.blob_key, d.owner_scope, v.created_at
                 FROM eval_datasets d
                 JOIN eval_dataset_versions v ON v.dataset_id = d.id
                WHERE d.owner_scope = $1
                ORDER BY v.created_at DESC, d.name, v.version
                LIMIT $2""",
            owner_scope,
            limit,
        )
        return [dict(row) for row in rows]

    async def get_eval_dataset_version(
        self,
        owner_scope: str,
        name: str,
        version: str,
    ) -> dict[str, Any] | None:
        row = await self.pool.fetchrow(
            """SELECT d.name, v.version, v.description, v.case_count,
                      v.content_hash, v.blob_key, d.owner_scope, v.created_at
                 FROM eval_datasets d
                 JOIN eval_dataset_versions v ON v.dataset_id = d.id
                WHERE d.owner_scope = $1 AND d.name = $2 AND v.version = $3""",
            owner_scope,
            name,
            version,
        )
        return dict(row) if row else None

    async def create_eval_suite(
        self,
        *,
        owner_scope: str,
        tenant_id: str,
        user_id: str,
        name: str,
        version: str,
        description: str,
        dataset_name: str,
        dataset_version: str,
        scorers: list[str],
        thresholds: dict[str, Any],
    ) -> dict[str, Any] | None:
        async with self.pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                """WITH selected AS (
                         SELECT dv.id AS dataset_version_id
                           FROM eval_datasets d
                           JOIN eval_dataset_versions dv ON dv.dataset_id = d.id
                          WHERE d.owner_scope = $1 AND d.name = $7 AND dv.version = $8
                       ), inserted AS (
                         INSERT INTO eval_suites
                             (owner_scope, tenant_id, user_id, name, version, description,
                              dataset_version_id, scorers, thresholds)
                         SELECT $1, $2, $3, $4, $5, $6,
                                selected.dataset_version_id, $9::text[], $10::jsonb
                           FROM selected
                         ON CONFLICT (owner_scope, name, version) DO NOTHING
                         RETURNING *
                       )
                       SELECT i.name, i.version, i.description,
                              d.name AS dataset_name, dv.version AS dataset_version,
                              i.scorers, i.thresholds, i.owner_scope, i.created_at
                         FROM inserted i
                         JOIN eval_dataset_versions dv ON dv.id = i.dataset_version_id
                         JOIN eval_datasets d ON d.id = dv.dataset_id""",
                owner_scope,
                tenant_id,
                user_id,
                name,
                version,
                description,
                dataset_name,
                dataset_version,
                scorers,
                json.dumps(thresholds),
            )
        return dict(row) if row else None

    async def list_eval_suites(self, owner_scope: str, *, limit: int = 100) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            """SELECT s.name, s.version, s.description,
                      d.name AS dataset_name, dv.version AS dataset_version,
                      s.scorers, s.thresholds, s.owner_scope, s.created_at
                 FROM eval_suites s
                 JOIN eval_dataset_versions dv ON dv.id = s.dataset_version_id
                 JOIN eval_datasets d ON d.id = dv.dataset_id
                WHERE s.owner_scope = $1
                ORDER BY s.created_at DESC, s.name, s.version
                LIMIT $2""",
            owner_scope,
            limit,
        )
        return [dict(row) for row in rows]

    async def get_eval_suite(self, owner_scope: str, name: str, version: str) -> dict[str, Any] | None:
        row = await self.pool.fetchrow(
            """SELECT s.name, s.version, s.description,
                      d.name AS dataset_name, dv.version AS dataset_version,
                      s.scorers, s.thresholds, s.owner_scope, s.created_at
                 FROM eval_suites s
                 JOIN eval_dataset_versions dv ON dv.id = s.dataset_version_id
                 JOIN eval_datasets d ON d.id = dv.dataset_id
                WHERE s.owner_scope = $1 AND s.name = $2 AND s.version = $3""",
            owner_scope,
            name,
            version,
        )
        return dict(row) if row else None

    async def save_eval_run(self, run: dict[str, Any]) -> None:
        owner_scope = str(run["owner_scope"])
        dataset = run.get("dataset") or None
        suite = run.get("suite") or None
        dataset_version_id = None
        suite_id = None
        async with self.pool.acquire() as connection, connection.transaction():
            if suite:
                if not dataset:
                    raise RuntimeError("eval suite run is missing its pinned dataset version")
                resolved = await connection.fetchrow(
                    """SELECT s.id AS suite_id, dv.id AS dataset_version_id
                         FROM eval_suites s
                         JOIN eval_dataset_versions dv ON dv.id = s.dataset_version_id
                         JOIN eval_datasets d ON d.id = dv.dataset_id
                        WHERE s.owner_scope = $1
                          AND s.name = $2 AND s.version = $3
                          AND d.name = $4 AND dv.version = $5""",
                    owner_scope,
                    suite["name"],
                    suite["version"],
                    dataset["name"],
                    dataset["version"],
                )
                if resolved is not None:
                    suite_id = resolved["suite_id"]
                    dataset_version_id = resolved["dataset_version_id"]
            elif dataset:
                resolved = await connection.fetchrow(
                    """SELECT dv.id AS dataset_version_id
                         FROM eval_datasets d
                         JOIN eval_dataset_versions dv ON dv.dataset_id = d.id
                        WHERE d.owner_scope = $1 AND d.name = $2 AND dv.version = $3""",
                    owner_scope,
                    dataset["name"],
                    dataset["version"],
                )
                if resolved is not None:
                    dataset_version_id = resolved["dataset_version_id"]
            await connection.execute(
                """INSERT INTO eval_runs
                         (id, owner_scope, tenant_id, user_id, sandbox_id, agent,
                          configuration, dataset_version_id, suite_id, dataset_ref,
                          suite_ref, summary, created_at)
                       VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9,
                               $10::jsonb, $11::jsonb, $12::jsonb, $13)""",
                run["id"],
                owner_scope,
                run.get("tenant_id") or "",
                run.get("user_id") or "",
                run["sandbox_id"],
                run.get("agent") or "",
                json.dumps(run.get("configuration") or {}),
                dataset_version_id,
                suite_id,
                json.dumps(dataset) if dataset else None,
                json.dumps(suite) if suite else None,
                json.dumps(run.get("summary") or {}),
                datetime.fromisoformat(str(run["created_at"])),
            )
            for case_index, result in enumerate(run.get("results") or []):
                await connection.execute(
                    """INSERT INTO eval_case_results
                             (eval_run_id, case_index, case_id, passed, result)
                           VALUES ($1, $2, $3, $4, $5::jsonb)""",
                    run["id"],
                    case_index,
                    result.get("name") or f"case-{case_index}",
                    bool(result.get("passed")),
                    json.dumps(result),
                )

    async def get_eval_run(self, owner_scope: str, sandbox_id: str, run_id: str) -> dict[str, Any] | None:
        row = await self.pool.fetchrow(
            self._eval_run_select()
            + " WHERE r.owner_scope = $1 AND r.sandbox_id = $2 AND r.id = $3"
            + self._eval_run_group(),
            owner_scope,
            sandbox_id,
            run_id,
        )
        return self._eval_run_record(row) if row else None

    async def get_eval_run_by_id(self, owner_scope: str, run_id: str) -> dict[str, Any] | None:
        row = await self.pool.fetchrow(
            self._eval_run_select() + " WHERE r.owner_scope = $1 AND r.id = $2" + self._eval_run_group(),
            owner_scope,
            run_id,
        )
        return self._eval_run_record(row) if row else None

    async def list_eval_runs(
        self,
        owner_scope: str,
        sandbox_id: str,
        *,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            self._eval_run_select()
            + " WHERE r.owner_scope = $1 AND r.sandbox_id = $2"
            + self._eval_run_group()
            + " ORDER BY r.created_at DESC LIMIT $3",
            owner_scope,
            sandbox_id,
            limit,
        )
        return [self._eval_run_record(row) for row in rows]

    async def create_eval_comparison(self, **values: Any) -> dict[str, Any]:
        async with self.pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                """INSERT INTO eval_comparisons
                         (id, owner_scope, tenant_id, user_id, baseline_run_id,
                          candidate_run_id, axes, result)
                       SELECT $1, $2, $3, $4, baseline.id, candidate.id,
                              $7::jsonb, $8::jsonb
                         FROM eval_runs baseline
                         JOIN eval_runs candidate ON candidate.id = $6
                        WHERE baseline.id = $5
                          AND baseline.owner_scope = $2
                          AND candidate.owner_scope = $2
                       ON CONFLICT (id) DO UPDATE
                         SET result = eval_comparisons.result
                       WHERE eval_comparisons.owner_scope = EXCLUDED.owner_scope
                         AND eval_comparisons.baseline_run_id = EXCLUDED.baseline_run_id
                         AND eval_comparisons.candidate_run_id = EXCLUDED.candidate_run_id
                       RETURNING id, result, created_at""",
                values["id"],
                values["owner_scope"],
                values.get("tenant_id") or "",
                values.get("user_id") or "",
                values["baseline_run_id"],
                values["candidate_run_id"],
                json.dumps(values.get("axes") or []),
                json.dumps(values.get("result") or {}),
            )
        if row is None:
            raise RuntimeError("owned evaluation runs not found")
        result = dict(row)
        if isinstance(result.get("result"), str):
            result["result"] = json.loads(result["result"])
        return result

    async def get_eval_comparison(self, owner_scope: str, comparison_id: str) -> dict[str, Any] | None:
        row = await self.pool.fetchrow(
            """SELECT id, result, created_at
                 FROM eval_comparisons
                WHERE owner_scope = $1 AND id = $2""",
            owner_scope,
            comparison_id,
        )
        if row is None:
            return None
        result = dict(row)
        if isinstance(result.get("result"), str):
            result["result"] = json.loads(result["result"])
        return result

    @staticmethod
    def _eval_run_select() -> str:
        return """SELECT r.id, r.owner_scope, r.tenant_id, r.user_id,
                         r.sandbox_id, r.agent, r.configuration, r.created_at, r.summary,
                         COALESCE(
                           CASE WHEN dv.id IS NULL THEN NULL
                                ELSE jsonb_build_object('name', d.name, 'version', dv.version) END,
                           r.dataset_ref
                         ) AS dataset,
                         COALESCE(
                           CASE WHEN s.id IS NULL THEN NULL
                                ELSE jsonb_build_object('name', s.name, 'version', s.version) END,
                           r.suite_ref
                         ) AS suite,
                         COALESCE(
                           jsonb_agg(cr.result ORDER BY cr.case_index)
                             FILTER (WHERE cr.id IS NOT NULL),
                           '[]'::jsonb
                         ) AS results
                    FROM eval_runs r
                    LEFT JOIN eval_dataset_versions dv ON dv.id = r.dataset_version_id
                    LEFT JOIN eval_datasets d ON d.id = dv.dataset_id
                    LEFT JOIN eval_suites s ON s.id = r.suite_id
                    LEFT JOIN eval_case_results cr ON cr.eval_run_id = r.id"""

    @staticmethod
    def _eval_run_group() -> str:
        return """ GROUP BY r.id, r.owner_scope, r.tenant_id, r.user_id,
                            r.sandbox_id, r.agent, r.configuration, r.created_at, r.summary,
                            r.dataset_ref, r.suite_ref,
                            dv.id, d.name, dv.version, s.id, s.name, s.version"""

    @staticmethod
    def _eval_run_record(row: Any) -> dict[str, Any]:
        result = dict(row)
        for key in ("configuration", "summary", "dataset", "suite", "results"):
            value = result.get(key)
            if isinstance(value, str):
                result[key] = json.loads(value)
        return result

    # =========================================================================
    # Agent sandboxes — one row per pinned agent configuration (#179)
    #
    # DDL lives in tesserix-k8s db-schema-bootstrap (sandboxes). The spec is
    # stored whole as JSONB because it is immutable once created: the row is the
    # record of what an eval ran against.
    # =========================================================================

    async def create_sandbox(
        self,
        sandbox_id: str,
        owner: str,
        spec: dict[str, Any],
        status: str,
        created_at: datetime,
        expires_at: datetime,
        last_access_at: datetime | None = None,
        tenant_id: str = "",
        user_id: str = "",
        max_live_per_tenant: int = 0,
        monthly_cost_limit_usd: float = 0.0,
    ) -> None:
        quota_key = tenant_id or user_id or owner
        async with self.pool.acquire() as connection, connection.transaction():
            if quota_key and (max_live_per_tenant > 0 or monthly_cost_limit_usd > 0):
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    f"sandbox-quota:{quota_key}",
                )
            if max_live_per_tenant > 0:
                live = await connection.fetchval(
                    """SELECT COUNT(*)
                         FROM sandboxes
                        WHERE status NOT IN ('destroyed', 'failed')
                          AND (($1 <> '' AND split_part(owner, ':', 1) = $1)
                               OR ($1 = '' AND owner = $2))""",
                    tenant_id,
                    owner,
                )
                if int(live or 0) >= max_live_per_tenant:
                    scope = f"tenant {tenant_id}" if tenant_id else f"user {user_id or owner}"
                    raise SandboxQuotaExceeded(f"{scope} reached its concurrent sandbox quota ({max_live_per_tenant})")
            if monthly_cost_limit_usd > 0:
                spent = await connection.fetchval(
                    """SELECT COALESCE(SUM(llm_cost_usd), 0)
                         FROM agent_executions
                        WHERE run_id LIKE 'sandbox:%'
                          AND started_at >= date_trunc('month', CURRENT_TIMESTAMP)
                          AND (($1 <> '' AND (tenant_id = $1 OR (tenant_id = '' AND user_id = $3)))
                               OR ($1 = '' AND user_id = $2))""",
                    tenant_id,
                    user_id or owner,
                    owner,
                )
                if float(spent or 0.0) >= monthly_cost_limit_usd:
                    scope = f"tenant {tenant_id}" if tenant_id else f"user {user_id or owner}"
                    raise SandboxQuotaExceeded(
                        f"{scope} reached its monthly sandbox cost quota (${monthly_cost_limit_usd:.2f})"
                    )
            await connection.execute(
                """INSERT INTO sandboxes
                     (id, owner, spec, status, created_at, expires_at, last_access_at)
                   VALUES ($1, $2, $3::jsonb, $4, $5, $6, $7)""",
                sandbox_id,
                owner,
                json.dumps(spec),
                status,
                created_at,
                expires_at,
                last_access_at or created_at,
            )

    async def get_sandbox(self, sandbox_id: str) -> dict[str, Any] | None:
        row = await self.pool.fetchrow("SELECT * FROM sandboxes WHERE id = $1", sandbox_id)
        return dict(row) if row else None

    async def list_sandboxes(self, owner: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if owner:
            rows = await self.pool.fetch(
                "SELECT * FROM sandboxes WHERE owner = $1 ORDER BY created_at DESC LIMIT $2",
                owner,
                limit,
            )
        else:
            rows = await self.pool.fetch("SELECT * FROM sandboxes ORDER BY created_at DESC LIMIT $1", limit)
        return [dict(r) for r in rows]

    async def sandbox_counts(self) -> dict[str, int]:
        rows = await self.pool.fetch("SELECT status, COUNT(*) AS n FROM sandboxes GROUP BY status")
        return {r["status"]: r["n"] for r in rows}

    async def set_sandbox_status(self, sandbox_id: str, status: str, detail: dict[str, Any] | None = None) -> None:
        await self.pool.execute(
            "UPDATE sandboxes SET status = $2, detail = COALESCE($3::jsonb, detail), updated_at = NOW() WHERE id = $1",
            sandbox_id,
            status,
            json.dumps(detail) if detail else None,
        )

    async def touch_sandbox(self, sandbox_id: str) -> None:
        await self.pool.execute("UPDATE sandboxes SET last_access_at = NOW() WHERE id = $1", sandbox_id)

    async def expired_sandboxes(self, now: datetime, limit: int = 100) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            "SELECT * FROM sandboxes WHERE status <> 'destroyed' AND expires_at <= $1 LIMIT $2",
            now,
            limit,
        )
        return [dict(r) for r in rows]


# ─────────────────────────────────────────────────────────────────────────
# Process-global analytics writer
# ─────────────────────────────────────────────────────────────────────────
#
# The LLM instrumentation (adapters/llm/instrumented.py) and the turn-usage
# accountant (pipeline/service.py) run deep inside call stacks that have no
# StageDeps or app.state. Like the usage ledger, they reach the database
# through this lazily-built process-global — built once from settings, never
# raising, None when Postgres isn't configured.

_GLOBAL_DB: Database | None = None
_GLOBAL_DB_FAILED = False


async def get_global_db() -> Database | None:
    """Lazily-connected process-global Database; None when unavailable."""
    global _GLOBAL_DB, _GLOBAL_DB_FAILED
    if _GLOBAL_DB is not None:
        return _GLOBAL_DB
    if _GLOBAL_DB_FAILED:
        return None
    try:
        from devai.config import settings

        dsn = getattr(settings, "database_url", "") or ""
        if not dsn:
            _GLOBAL_DB_FAILED = True
            return None
        db = Database(dsn)
        await db.connect()
        _GLOBAL_DB = db
        return db
    except Exception:  # noqa: BLE001 — analytics persistence is best-effort
        logger.info("global analytics db unavailable — Postgres rollups disabled", exc_info=True)
        _GLOBAL_DB_FAILED = True
        return None


def set_global_db(db: Database | None) -> None:
    """Test/bootstrap hook — inject or clear the process-global Database."""
    global _GLOBAL_DB, _GLOBAL_DB_FAILED
    _GLOBAL_DB = db
    _GLOBAL_DB_FAILED = False
