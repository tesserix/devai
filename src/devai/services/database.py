"""PostgreSQL database service for persistent ALM lifecycle storage.

Dual-store architecture:
  PostgreSQL — persistent, queryable, auditable (runs, agents, A2A, memory, audit)
  Redis      — ephemeral, fast (locks, sessions, live state, pub/sub)

Uses asyncpg for async PostgreSQL access.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from datetime import datetime

logger = logging.getLogger(__name__)


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
        raise RuntimeError(
            f"PostgreSQL pool init failed after {attempts} attempts: {last}"
        )

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
    ) -> None:
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
    ) -> list[dict[str, Any]]:
        """Find memories by semantic similarity using pgvector."""
        if repo:
            rows = await self.pool.fetch(
                """SELECT *, 1 - (embedding <=> $1::vector) AS similarity
                   FROM agent_memories
                   WHERE is_active = TRUE AND embedding IS NOT NULL
                     AND (repo = $2 OR repo = 'global')
                   ORDER BY embedding <=> $1::vector
                   LIMIT $3""",
                embedding,
                repo,
                limit,
            )
        else:
            rows = await self.pool.fetch(
                """SELECT *, 1 - (embedding <=> $1::vector) AS similarity
                   FROM agent_memories
                   WHERE is_active = TRUE AND embedding IS NOT NULL
                   ORDER BY embedding <=> $1::vector
                   LIMIT $2""",
                embedding,
                limit,
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
