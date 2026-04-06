"""Database Engineer Agent — manages schemas, migrations, and data integrity.

Runs AFTER the Senior Developer implements code. Detects database changes
in the implementation, generates proper migration scripts using Liquibase
(or framework-native tools), validates schema evolution, and ensures:

1. No destructive migrations without explicit approval
2. Migration history is never deleted or rewritten
3. Rollback scripts exist for every forward migration
4. Schema changes are audited and logged
5. Data integrity constraints are maintained

Supports:
- Liquibase (XML/YAML/SQL changesets)
- Alembic (Python/SQLAlchemy)
- Prisma Migrate
- GORM AutoMigrate → converted to proper migrations
- Raw SQL migrations (versioned)
"""

from __future__ import annotations

import json
import logging
from typing import Any

from devai.core.base_agent import BaseAgent
from devai.graph.a2a import A2ABus
from devai.graph.state import ALMState
from devai.providers.anthropic_claude import ClaudeProvider
from devai.tools.github_tools import GITHUB_TOOLS, GitHubToolExecutor

logger = logging.getLogger(__name__)

DB_TOOLS = [
    t for t in GITHUB_TOOLS
    if t["name"] in {
        "github_get_file_content",
        "github_list_files",
        "github_get_repo_tree",
        "github_get_pr_diff",
        "github_commit_file",
        "github_add_comment",
    }
]

SYSTEM_PROMPT = """You are a Principal Database Engineer responsible for schema management and data integrity in the DevAI ALM pipeline.

## Your Responsibilities

1. **Detect Schema Changes**: Analyze the PR diff for any database-related changes:
   - New tables, columns, indexes, constraints
   - Modified column types, nullability, defaults
   - Dropped tables or columns (DANGEROUS — requires approval)
   - New or modified ORM models (GORM, SQLAlchemy, Prisma, TypeORM)

2. **Generate Migration Scripts**: Create proper, versioned migration files:
   - For Liquibase: XML/YAML changesets with rollback tags
   - For Alembic: Python migration with upgrade() and downgrade()
   - For Prisma: prisma migrate dev
   - For raw SQL: numbered migration files with up/down

3. **Validate Migration Safety**:
   - NEVER generate DROP TABLE without explicit requirement
   - NEVER generate DROP COLUMN without data backup plan
   - ALWAYS include rollback/downgrade logic
   - ALWAYS preserve migration history (never delete old migrations)
   - Check for breaking changes (column rename = drop + add → data loss)
   - Validate foreign key references
   - Check for N+1 query patterns in new models

4. **Audit Trail**:
   - Every migration must have a description and author
   - Changes must be committed to the migration directory
   - Post a summary on the PR listing all schema changes

## Migration Patterns by Stack

### Go + GORM
- Look for `gorm.Model` structs and `AutoMigrate()` calls
- Generate SQL migrations in `migrations/` directory
- Numbered format: `000X_description.up.sql` / `000X_description.down.sql`

### Python + SQLAlchemy/Alembic
- Look for `Base = declarative_base()` and model changes
- Generate Alembic migration: `alembic/versions/XXXX_description.py`
- Include `upgrade()` and `downgrade()` functions

### Node.js + Prisma
- Look for `prisma/schema.prisma` changes
- Generate migration SQL in `prisma/migrations/`

### Liquibase (any stack)
- Generate changeset in `db/changelog/` directory
- Include rollback section for every changeset
- Use sequential changeset IDs

## Critical Rules

1. **NEVER delete migration history** — old migrations must remain intact
2. **NEVER use DROP without explicit requirement** — flag and escalate
3. **ALWAYS generate rollback scripts** — every up needs a down
4. **ALWAYS validate foreign keys** — broken references cause runtime errors
5. **ALWAYS check for data loss** — column type changes, drops, truncates
6. **Audit every change** — post detailed summary on PR

## Output

After analyzing and generating migrations:
1. Commit migration files to the PR branch
2. Post a DB Change Summary on the PR as a comment
3. Report decision: SAFE / REVIEW_NEEDED / BLOCKED

If BLOCKED: escalate to Engineering Manager with specific concerns.
"""


class DBEngineerAgent(BaseAgent):
    """Database Engineer — handles schema migrations and data integrity."""

    name = "db_engineer"
    subscribe_subject = "devai.pipeline.code_implemented"
    publish_subject = "devai.pipeline.db_migrated"

    async def _execute_graph(self, state: ALMState, a2a: A2ABus) -> dict[str, Any]:
        """Analyze code for DB changes and generate migrations."""
        claude = ClaudeProvider(self.config)
        tool_executor = GitHubToolExecutor(self.github)

        repo = state.get("repo_full_name", "")
        pr_number = state.get("pr_number")
        branch = state.get("branch_name", "")
        plan = state.get("technical_plan", "")

        if not pr_number or not branch:
            # No PR yet — nothing to analyze
            return {"db_decision": "not_applicable"}

        # Check for A2A messages
        inbox_context = a2a.format_inbox_context()

        user_message = f"""Repository: {repo}
PR: #{pr_number}
Branch: {branch}

## Technical Plan
{plan[:2000]}

{inbox_context}

Analyze the PR for database/schema changes:
1. Get the PR diff to find model/schema/migration changes
2. Explore the repo tree for existing migration patterns
3. If schema changes detected:
   a. Read existing migrations to understand the pattern
   b. Generate proper migration files (with rollback)
   c. Commit them to the branch
   d. Post a DB Change Summary on the PR
4. If no schema changes, confirm and move on

CRITICAL: Never delete existing migration files. Never generate DROP without explicit approval.
Always include rollback/downgrade scripts."""

        # Inject governance
        system = SYSTEM_PROMPT
        governance = state.get("governance", "")
        if governance:
            system += f"\n\n## Repository Governance\n{governance}"

        result_text = await claude.run_agent_loop(
            system_prompt=system,
            user_message=user_message,
            tools=DB_TOOLS,
            tool_executor=tool_executor.execute,
        )

        # Parse decision
        decision = self._parse_decision(result_text)

        # A2A communication
        if decision == "blocked":
            a2a.escalate(
                "engineering_manager",
                "DB Migration BLOCKED — Destructive Change Detected",
                f"PR #{pr_number} contains potentially destructive database changes.\n\n"
                f"{result_text[:500]}",
            )
            a2a.escalate(
                "senior_developer",
                "DB Changes Need Revision",
                f"The Database Engineer blocked your changes. Please review:\n\n"
                f"{result_text[:300]}",
            )
        elif decision == "review_needed":
            a2a.notify(
                "staff_reviewer",
                "DB Changes Need Manual Review",
                f"Schema changes in PR #{pr_number} require careful review:\n\n"
                f"{result_text[:300]}",
            )
        else:
            a2a.notify(
                "staff_reviewer",
                "DB Migrations Generated",
                f"Schema changes validated and migration files committed for PR #{pr_number}.",
            )

        # Record in memory for future reference
        try:
            from devai.services.memory import AgentMemory

            memory = AgentMemory(self.state.redis)
            await memory.learn_repo_pattern(
                repo=repo,
                pattern_type="db_migration_pattern",
                description=f"DB migration pattern in {repo}: {result_text[:200]}",
            )
        except Exception:
            pass

        return {
            "db_decision": decision,
            "db_summary": result_text,
        }

    def _parse_decision(self, result_text: str) -> str:
        """Extract SAFE/REVIEW_NEEDED/BLOCKED from agent output."""
        text_upper = result_text.upper()

        if "BLOCKED" in text_upper or "DROP TABLE" in text_upper or "DESTRUCTIVE" in text_upper:
            return "blocked"
        if "REVIEW_NEEDED" in text_upper or "REVIEW NEEDED" in text_upper:
            return "review_needed"
        if "NOT_APPLICABLE" in text_upper or "NO SCHEMA CHANGES" in text_upper or "NO DATABASE" in text_upper:
            return "not_applicable"

        return "safe"
