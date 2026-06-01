# 08 — MCP Tool Ecosystem (host + client)

**Cursor parity:** MCP support. **Priority: P2.**

## What Cursor does

MCP (Model Context Protocol) is a plugin system: connect external tools/data
sources (DBs, APIs, services) to the agent. Lesson learned: there's a practical
ceiling (~40 active tools) before tool definitions blow the context budget and
the agent silently loses access — so the **tool surface must be budgeted**.

## How it works (concepts to steal)

1. **Standard protocol** to mount third‑party tools without bespoke glue.
2. **Dynamic tool discovery** — list tools from each connected server.
3. **Tool‑budget management** — gate/curate which tools are live per task
   (don't expose all 40 at once).
4. **Per‑server auth + scoping.**

## DevAI mapping (framework)

- **MCP client adapter** so DevAI agents can *consume* MCP servers: a tool
  provider that registers discovered MCP tools into the LangGraph/LangChain tool
  list. Goes through `adapters/` (lazy SDK import, noop fallback).
- **Tool‑budget layer** (shared with plan 06's budget guard): score tools by
  task relevance, expose top‑N, lazily load the rest on demand (mirrors how *this*
  very harness defers tool schemas).
- **agentgateway as MCP host:** `agentic/` + `devai-ai-gateway` already front LLM
  traffic; extend it to also broker MCP servers (central auth, allowlist, audit) —
  and the safety classifier (plan 07) gates MCP calls.
- **Registry tie‑in:** `adapters/registry` / agentregistry can catalogue available
  MCP servers per tenant.

## Implementation plan

- **Phase 1 — MCP client adapter** + dynamic tool registration.
- **Phase 2 — tool‑budget selector** (relevance rank + lazy load + cap).
- **Phase 3 — gateway brokering** (auth, allowlist, audit) + safety gating.
- **Phase 4 — registry catalogue + per‑tenant enablement.**

## Files & modules

```
src/devai/adapters/mcp/{base,factory,client,noop}.py
src/devai/tools/mcp_tools.py            # register discovered tools
src/devai/agentic/*                     # gateway brokering
tests/unit/test_mcp_adapter.py
```

## Config (`DEVAI_*`)

```
DEVAI_MCP_ENABLED=true
DEVAI_MCP_SERVERS=[{"name":"jira","url":"...","auth":"..."}]
DEVAI_MCP_MAX_ACTIVE_TOOLS=30          # budget guard
```

## Acceptance criteria

- A configured MCP server's tools appear to agents and are callable.
- With >budget tools available, only top‑N load; the rest resolve on demand.
- MCP calls pass through the safety classifier and are audited.

## Sources

- [Cursor Docs — MCP](https://cursor.com/docs)
- [Cursor 2026 Guide — MCP · DeployHQ](https://www.deployhq.com/guides/cursor)
