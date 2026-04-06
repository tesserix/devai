"""DevAI Chat Agent — conversational interface to the entire platform.

A LangChain-powered ReAct agent that can query ALL platform data and
respond conversationally. Connected to:

  - Pipeline runs (history, status, timings)
  - A2A messages (inter-agent communication logs)
  - Agent memory (episodic, semantic, procedural)
  - Security findings (vulnerabilities, SBOM)
  - SRE incidents (cluster health, costs, performance)
  - GitHub data (issues, PRs, repos)
  - Audit log (who did what, when)

The agent uses tool-calling to fetch data on demand and synthesizes
it into natural language responses with proper formatting.

Architecture:
  User message
      ↓
  LangChain ChatAnthropic (Claude Sonnet 4)
      ↓
  Tool selection (ReAct loop)
      ├── query_pipeline_runs
      ├── query_pipeline_detail
      ├── query_a2a_messages
      ├── query_agent_memory
      ├── query_security_findings
      ├── query_sre_incidents
      ├── query_audit_log
      ├── query_cluster_health
      ├── query_cost_reports
      └── search_across_all
      ↓
  Synthesized response (markdown)
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import tool

from devai.services.tracing import traceable_if_enabled

if TYPE_CHECKING:

    from devai.config import Settings
    from devai.core.state import StateManager
    from devai.services.database import Database

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are the DevAI Assistant — an intelligent interface to the DevAI ALM and SRE platform.

You have access to the complete history of:
- All pipeline runs (requirements → epic → stories → code → review → security → deploy)
- Agent-to-agent (A2A) communication between all 13 ALM agents and 7 SRE agents
- Agent memory (what agents learned from past runs)
- Security scan results (SAST, SCA, secrets, OWASP)
- SRE incidents (infrastructure issues, performance, costs)
- Audit logs (every action taken by every agent)
- Cluster health and cost data

When answering:
1. Use the available tools to fetch real data — don't make up information
2. Format responses in clean markdown with tables, bullet points, and code blocks
3. Be specific — cite run IDs, issue numbers, agent names, timestamps
4. When showing metrics, include context (trend, threshold, comparison)
5. For security questions, always check actual findings — never assume "all clear"
6. For cost questions, show numbers with $ amounts and % changes

You also have full access to source code repositories (GitHub, GitLab, Azure DevOps):
- Read files, browse directory trees, view PRs and issues
- Use this to validate changes, explain code, or investigate problems

You can answer questions like:
- "What happened in the last pipeline run?"
- "Show me all security findings for tesserix/Home-Chef-App"
- "Why did the build fail yesterday?"
- "What are the current SRE incidents?"
- "How much is the cluster costing per day?"
- "What did the agents learn about this repo?"
- "Show me the code in src/main.go of tesserix/my-service"
- "What changed in PR #42?"
- "Give me a full status report of all projects"
"""


class DevAIChatAgent:
    """Conversational agent backed by LangChain + Claude."""

    def __init__(
        self,
        config: Settings,
        state_manager: StateManager,
        database: Database | None = None,
    ) -> None:
        self.config = config
        self.state = state_manager
        self.db = database
        self.llm = ChatAnthropic(
            model=config.claude_model,
            anthropic_api_key=config.anthropic_api_key,
            max_tokens=4096,
            temperature=0.3,
        )
        self._tools = self._build_tools()
        self._conversations: dict[str, list] = {}  # session_id -> message history

    def _build_tools(self) -> list:
        """Build LangChain tools that query platform data."""
        state = self.state
        db = self.db
        config = self.config

        @tool
        async def query_pipeline_runs(
            repo: str = "",
            limit: int = 10,
        ) -> str:
            """Query recent pipeline runs. Optionally filter by repo name. Returns run IDs, stages, repos, and timestamps."""
            if repo:
                run_ids = await state.list_runs_by_repo(repo, limit)
            else:
                run_ids = await state.list_runs(limit)

            runs = []
            for rid in run_ids:
                run_data = await state.get_run(rid)
                if run_data:
                    agents = await state.get_agent_statuses(rid)
                    completed = sum(1 for a in agents.values() if a.get("status") == "completed")
                    runs.append({
                        "run_id": rid,
                        "repo": run_data.get("repo", ""),
                        "stage": run_data.get("stage", ""),
                        "created": run_data.get("created_at", ""),
                        "agents_completed": f"{completed}/{len(agents)}",
                    })

            return json.dumps(runs, indent=2) if runs else "No pipeline runs found."

        @tool
        async def query_pipeline_detail(run_id: str) -> str:
            """Get detailed information about a specific pipeline run including agent statuses and timing."""
            run_data = await state.get_run(run_id)
            if not run_data:
                return f"Run {run_id} not found."

            agents = await state.get_agent_statuses(run_id)

            result = {
                "run_id": run_id,
                "repo": run_data.get("repo", ""),
                "stage": run_data.get("stage", ""),
                "created": run_data.get("created_at", ""),
                "agents": agents,
            }

            # Get context for more details
            ctx = run_data.get("context", {})
            if isinstance(ctx, dict):
                result["requirements"] = str(ctx.get("requirements", ""))[:500]
                result["branch"] = ctx.get("branch_name", "")
                result["pr_number"] = ctx.get("pr_number")
                result["review_iteration"] = ctx.get("review_iteration", 0)

            return json.dumps(result, indent=2, default=str)

        @tool
        async def query_a2a_messages(run_id: str, agent_filter: str = "") -> str:
            """Get A2A (agent-to-agent) messages for a pipeline run. Optionally filter by agent name."""
            raw = await state.redis.lrange(f"devai:run:{run_id}:a2a_messages", 0, -1)
            messages = [json.loads(m) for m in raw]

            if agent_filter:
                messages = [
                    m for m in messages
                    if m.get("from_agent") == agent_filter or m.get("to_agent") == agent_filter
                ]

            # Summarize for readability
            summary = []
            for m in messages[-30:]:  # Last 30
                summary.append({
                    "from": m.get("from_agent", "?"),
                    "to": m.get("to_agent", "?"),
                    "type": m.get("message_type", "?"),
                    "subject": m.get("subject", ""),
                    "body": m.get("body", "")[:200],
                })

            return json.dumps(summary, indent=2) if summary else "No A2A messages found."

        @tool
        async def query_agent_memory(
            agent: str = "",
            repo: str = "",
            memory_type: str = "",
            query: str = "",
            limit: int = 10,
        ) -> str:
            """Search agent memory. Filter by agent name, repo, type (episodic/semantic/procedural), or free-text query."""
            from devai.services.memory import AgentMemory

            memory = AgentMemory(state.redis)
            entries = await memory.recall(
                agent=agent or None,
                repo=repo or None,
                memory_type=memory_type or None,
                query=query or None,
                limit=limit,
            )

            results = []
            for e in entries:
                results.append({
                    "agent": e.agent,
                    "repo": e.repo,
                    "type": e.memory_type,
                    "content": e.content,
                    "tags": e.tags,
                    "access_count": e.access_count,
                    "age_days": round(((__import__("time").time() - e.created_at) / 86400), 1),
                })

            return json.dumps(results, indent=2) if results else "No memories found."

        @tool
        async def query_security_findings(
            repo: str = "",
            severity: str = "",
            status: str = "open",
            limit: int = 20,
        ) -> str:
            """Query security scan findings. Filter by repo, severity (critical/high/medium/low), and status (open/fixed)."""
            if not db:
                return "Database not connected — cannot query security findings."

            conditions = ["1=1"]
            params: list[Any] = []
            idx = 1

            if repo:
                conditions.append(f"repo = ${idx}")
                params.append(repo)
                idx += 1
            if severity:
                conditions.append(f"severity = ${idx}")
                params.append(severity)
                idx += 1
            if status:
                conditions.append(f"status = ${idx}")
                params.append(status)
                idx += 1

            params.append(limit)
            where = " AND ".join(conditions)

            rows = await db.pool.fetch(
                f"SELECT * FROM security_findings WHERE {where} ORDER BY created_at DESC LIMIT ${idx}",
                *params,
            )

            return json.dumps([dict(r) for r in rows], indent=2, default=str) if rows else "No security findings found."

        @tool
        async def query_sre_incidents(
            status: str = "open",
            severity: str = "",
            limit: int = 20,
        ) -> str:
            """Query SRE incidents. Filter by status (open/investigating/resolved) and severity."""
            if not db:
                return "Database not connected — cannot query SRE incidents."

            conditions = []
            params: list[Any] = []
            idx = 1

            if status:
                conditions.append(f"status = ${idx}")
                params.append(status)
                idx += 1
            if severity:
                conditions.append(f"severity = ${idx}")
                params.append(severity)
                idx += 1

            params.append(limit)
            where = " AND ".join(conditions) if conditions else "1=1"

            rows = await db.pool.fetch(
                f"SELECT * FROM sre_incidents WHERE {where} ORDER BY created_at DESC LIMIT ${idx}",
                *params,
            )

            return json.dumps([dict(r) for r in rows], indent=2, default=str) if rows else "No SRE incidents found."

        @tool
        async def query_audit_log(
            run_id: str = "",
            agent: str = "",
            action: str = "",
            limit: int = 30,
        ) -> str:
            """Query the audit log. Every action by every agent is recorded here. Filter by run_id, agent name, or action type."""
            if not db:
                return "Database not connected — cannot query audit log."

            conditions = []
            params: list[Any] = []
            idx = 1

            if run_id:
                conditions.append(f"run_id = ${idx}")
                params.append(run_id)
                idx += 1
            if agent:
                conditions.append(f"agent_name = ${idx}")
                params.append(agent)
                idx += 1
            if action:
                conditions.append(f"action ILIKE ${idx}")
                params.append(f"%{action}%")
                idx += 1

            params.append(limit)
            where = " AND ".join(conditions) if conditions else "1=1"

            rows = await db.pool.fetch(
                f"SELECT * FROM audit_log WHERE {where} ORDER BY created_at DESC LIMIT ${idx}",
                *params,
            )

            return json.dumps([dict(r) for r in rows], indent=2, default=str) if rows else "No audit entries found."

        @tool
        async def query_cluster_health() -> str:
            """Get the current health of all monitored Kubernetes clusters including open incidents, app count, and daily cost."""
            if not db:
                return "Database not connected — cannot query cluster health."

            rows = await db.pool.fetch("SELECT * FROM v_sre_cluster_health")
            return json.dumps([dict(r) for r in rows], indent=2, default=str) if rows else "No cluster data available."

        @tool
        async def query_cost_reports(days: int = 30) -> str:
            """Get cloud cost reports for the specified number of days. Shows daily costs with breakdown."""
            if not db:
                return "Database not connected — cannot query costs."

            rows = await db.pool.fetch(
                "SELECT * FROM sre_cost_reports WHERE report_date > CURRENT_DATE - $1 ORDER BY report_date DESC",
                days,
            )
            return json.dumps([dict(r) for r in rows], indent=2, default=str) if rows else "No cost data available."

        @tool
        async def search_across_all(query: str) -> str:
            """Search across all data sources — pipeline runs, A2A messages, memory, incidents — for a keyword or phrase."""
            results: dict[str, Any] = {}

            # Search memory
            from devai.services.memory import AgentMemory

            memory = AgentMemory(state.redis)
            mem_hits = await memory.recall(query=query, limit=5)
            if mem_hits:
                results["memory"] = [{"content": m.content, "agent": m.agent, "repo": m.repo} for m in mem_hits]

            # Search recent runs
            run_ids = await state.list_runs(20)
            matching_runs = []
            for rid in run_ids:
                run_data = await state.get_run(rid)
                if run_data:
                    ctx = run_data.get("context", {})
                    if isinstance(ctx, dict) and query.lower() in json.dumps(ctx).lower():
                        matching_runs.append({"run_id": rid, "repo": run_data.get("repo", ""), "stage": run_data.get("stage", "")})
            if matching_runs:
                results["pipeline_runs"] = matching_runs[:5]

            # Search DB if available
            if db:
                try:
                    incident_rows = await db.pool.fetch(
                        "SELECT id, title, severity, status FROM sre_incidents WHERE title ILIKE $1 OR description ILIKE $1 LIMIT 5",
                        f"%{query}%",
                    )
                    if incident_rows:
                        results["sre_incidents"] = [dict(r) for r in incident_rows]
                except Exception:
                    pass

            return json.dumps(results, indent=2, default=str) if results else f"No results found for '{query}'."

        # --- SCM / Source Code Tools ---

        @tool
        async def scm_get_file(repo: str, path: str, branch: str = "") -> str:
            """Read a file from a source code repository. Provide repo (org/repo), file path, and optional branch."""
            from devai.scm import create_scm_client

            scm = create_scm_client(config)
            try:
                content = await scm.get_file_content(repo, path, ref=branch or None)
                return content[:8000]  # Limit to 8K chars
            except Exception as e:
                return f"Error reading {path}: {e}"
            finally:
                await scm.close()

        @tool
        async def scm_list_files(repo: str, path: str = "", branch: str = "") -> str:
            """List files in a directory of a source code repository."""
            from devai.scm import create_scm_client

            scm = create_scm_client(config)
            try:
                files = await scm.list_files(repo, path, ref=branch or None)
                if isinstance(files, list):
                    return json.dumps([{"name": f.get("name", f.get("path", "")), "type": f.get("type", "")} for f in files[:50]], indent=2)
                return str(files)
            except Exception as e:
                return f"Error listing files: {e}"
            finally:
                await scm.close()

        @tool
        async def scm_get_repo_tree(repo: str) -> str:
            """Get the full file tree of a repository. Returns all file paths."""
            from devai.scm import create_scm_client

            scm = create_scm_client(config)
            try:
                tree = await scm.get_repo_tree(repo)
                paths = [item.get("path", "") for item in tree if item.get("type") in ("blob", "file")]
                return "\n".join(paths[:200])  # First 200 files
            except Exception as e:
                return f"Error: {e}"
            finally:
                await scm.close()

        @tool
        async def scm_get_pr(repo: str, pr_number: int) -> str:
            """Get pull request details including title, body, status, and diff summary."""
            from devai.scm import create_scm_client

            scm = create_scm_client(config)
            try:
                pr = await scm.get_pull_request(repo, pr_number)
                diff = await scm.get_pr_diff(repo, pr_number)
                return json.dumps({
                    "number": pr.get("number") or pr.get("iid"),
                    "title": pr.get("title", ""),
                    "state": pr.get("state", ""),
                    "body": pr.get("body", pr.get("description", ""))[:1000],
                    "diff_preview": diff[:3000],
                }, indent=2, default=str)
            except Exception as e:
                return f"Error: {e}"
            finally:
                await scm.close()

        @tool
        async def scm_get_issue(repo: str, issue_number: int) -> str:
            """Get a GitHub/GitLab issue with title, body, labels, and comments."""
            from devai.scm import create_scm_client

            scm = create_scm_client(config)
            try:
                issue = await scm.get_issue(repo, issue_number)
                return json.dumps({
                    "number": issue.get("number") or issue.get("iid"),
                    "title": issue.get("title", ""),
                    "state": issue.get("state", ""),
                    "labels": [lbl.get("name", lbl) if isinstance(lbl, dict) else lbl for lbl in issue.get("labels", [])],
                    "body": issue.get("body", issue.get("description", ""))[:2000],
                }, indent=2, default=str)
            except Exception as e:
                return f"Error: {e}"
            finally:
                await scm.close()

        return [
            query_pipeline_runs,
            query_pipeline_detail,
            query_a2a_messages,
            query_agent_memory,
            query_security_findings,
            query_sre_incidents,
            query_audit_log,
            query_cluster_health,
            query_cost_reports,
            search_across_all,
            scm_get_file,
            scm_list_files,
            scm_get_repo_tree,
            scm_get_pr,
            scm_get_issue,
        ]

    @traceable_if_enabled(name="chat.respond", run_type="chain")
    async def chat(
        self,
        message: str,
        session_id: str = "default",
    ) -> str:
        """Process a user message and return a response.

        Maintains conversation history per session. Uses LangChain's
        tool-calling with Claude to query platform data and synthesize
        natural language responses.
        """
        # Get or create conversation history
        if session_id not in self._conversations:
            self._conversations[session_id] = [
                SystemMessage(content=SYSTEM_PROMPT),
            ]

        history = self._conversations[session_id]
        history.append(HumanMessage(content=message))

        # Bind tools to the model
        llm_with_tools = self.llm.bind_tools(self._tools)

        # ReAct loop: call LLM → execute tools → feed back → repeat
        for _iteration in range(8):  # Max 8 tool-calling rounds
            response = await llm_with_tools.ainvoke(history)
            history.append(response)

            # Check for tool calls
            if not response.tool_calls:
                # No tool calls — final response
                break

            # Execute each tool call
            from langchain_core.messages import ToolMessage

            for tc in response.tool_calls:
                tool_fn = next((t for t in self._tools if t.name == tc["name"]), None)
                if tool_fn:
                    try:
                        result = await tool_fn.ainvoke(tc["args"])
                        history.append(ToolMessage(
                            content=str(result),
                            tool_call_id=tc["id"],
                        ))
                    except Exception as e:
                        logger.error("Tool %s failed: %s", tc["name"], e)
                        history.append(ToolMessage(
                            content=f"Error: {e}",
                            tool_call_id=tc["id"],
                        ))

        # Extract final text response
        final = history[-1]
        response_text = final.content if isinstance(final, AIMessage) else str(final)

        # Trim conversation to last 30 messages to prevent context overflow
        if len(history) > 32:
            self._conversations[session_id] = [history[0]] + history[-30:]

        return response_text

    async def stream_chat(
        self,
        message: str,
        session_id: str = "default",
    ):
        """Stream a response token by token.

        Yields text chunks as they arrive from Claude.
        For tool calls, yields a status update then continues.
        """
        if session_id not in self._conversations:
            self._conversations[session_id] = [
                SystemMessage(content=SYSTEM_PROMPT),
            ]

        history = self._conversations[session_id]
        history.append(HumanMessage(content=message))

        llm_with_tools = self.llm.bind_tools(self._tools)

        for _iteration in range(8):
            # Collect full response for tool handling
            response = await llm_with_tools.ainvoke(history)
            history.append(response)

            if not response.tool_calls:
                # Stream the final text
                if isinstance(response.content, str):
                    yield response.content
                elif isinstance(response.content, list):
                    for block in response.content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            yield block["text"]
                        elif isinstance(block, str):
                            yield block
                break

            # Execute tools and show status
            from langchain_core.messages import ToolMessage

            for tc in response.tool_calls:
                yield f"\n> _Querying {tc['name']}..._\n"

                tool_fn = next((t for t in self._tools if t.name == tc["name"]), None)
                if tool_fn:
                    try:
                        result = await tool_fn.ainvoke(tc["args"])
                        history.append(ToolMessage(content=str(result), tool_call_id=tc["id"]))
                    except Exception as e:
                        history.append(ToolMessage(content=f"Error: {e}", tool_call_id=tc["id"]))

        # Trim
        if len(history) > 32:
            self._conversations[session_id] = [history[0]] + history[-30:]

    def clear_session(self, session_id: str = "default") -> None:
        """Clear conversation history for a session."""
        self._conversations.pop(session_id, None)
