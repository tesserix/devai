-- Rollback: Drop all DevAI tables
-- WARNING: This will delete all data. Only use in development.

DROP VIEW IF EXISTS v_security_posture;
DROP VIEW IF EXISTS v_agent_stats;
DROP VIEW IF EXISTS v_recent_activity;

DROP TABLE IF EXISTS security_findings;
DROP TABLE IF EXISTS db_migration_audit;
DROP TABLE IF EXISTS approval_gates;
DROP TABLE IF EXISTS audit_log;
DROP TABLE IF EXISTS agent_memories;
DROP TABLE IF EXISTS a2a_messages;
DROP TABLE IF EXISTS agent_executions;
DROP TABLE IF EXISTS pipeline_config;
DROP TABLE IF EXISTS pipeline_runs;

DROP EXTENSION IF EXISTS pgvector;
DROP EXTENSION IF EXISTS "uuid-ossp";
