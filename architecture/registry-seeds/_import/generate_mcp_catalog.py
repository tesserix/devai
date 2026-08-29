#!/usr/bin/env python3
"""Generate the curated MCP marketplace catalog (kind: MCPServer templates).

Each entry is a *catalog template* — a browse-and-connect card in the Settings
→ MCP Marketplace, NOT a shared downstream leg. The hub skips anything labelled
``mcp.devai.io/catalog: "true"`` (see mcphub/discovery.discover); a template
reaches a user only after they connect it (per-user federation).

Transport is ALWAYS streamable-http over HTTPS — DevAI's proxy/mesh layers
expect it. A server that ships natively over stdio (drawio, filesystem,
postgres, …) is marked ``native: stdio`` with a ``stdio`` launch spec; the
devai-mcp-bridge runs that process and fronts it as streamable-http, so the
hub still only ever dials https.

Auth kinds:
  token  — the user pastes a PAT / API key (works today via the MCP connector)
  oauth  — hosted server behind OAuth 2.1 (Connect-with-OAuth flow)
  none   — no credential (e.g. a public docs server)
  env    — stdio server whose secret is an env var (connection string, key)

Run: python architecture/registry-seeds/_import/generate_mcp_catalog.py
"""

from __future__ import annotations

from pathlib import Path

import yaml

SEEDS = Path(__file__).resolve().parents[1]
OUT = SEEDS / "mcp-servers"
BRIDGE = "http://devai-mcp-bridge.devai.svc.cluster.local:8099/bridge/"


def _http(name, display, category, auth, endpoint, credential, docs, description):
    return {
        "name": name,
        "display": display,
        "category": category,
        "native": "http",
        "auth": auth,
        "endpoint": endpoint,
        "credential": credential,
        "docs": docs,
        "description": description,
    }


def _stdio(name, display, category, auth, command, args, env, credential, docs, description):
    spec = {"command": command, "args": args}
    if env:
        spec["env"] = env
    return {
        "name": name,
        "display": display,
        "category": category,
        "native": "stdio",
        "auth": auth,
        "stdio": spec,
        "credential": credential,
        "docs": docs,
        "description": description,
    }


def _npx(
    name, display, category, pkg, *, auth="none", env=None, extra_args=None, credential="none", docs="", description=""
):
    return _stdio(
        name,
        display,
        category,
        auth,
        "npx",
        ["-y", pkg, *(extra_args or [])],
        env or {},
        credential,
        docs or f"https://www.npmjs.com/package/{pkg}",
        description,
    )


# The curated directory. Transport to the hub is ALWAYS streamable-https; stdio
# servers run in the bridge. Endpoints/packages reflect each vendor's published
# MCP as of the build; adding a tool is a one-line data entry here.
CATALOG: list[dict] = [
    # ── Universal bridge ────────────────────────────────────────────────
    _http(
        "zapier",
        "Zapier (8,000+ apps)",
        "automation",
        "oauth",
        "https://mcp.zapier.com/api/mcp/mcp",
        "Zapier OAuth — then enable the actions/apps you want exposed",
        "https://zapier.com/mcp",
        "The universal bridge — one connection reaches 8,000+ apps (Lucidchart, Bolt, "
        "Lovable, Calendly, Airtable, Trello, and the long tail) via Zapier actions.",
    ),
    _http(
        "make",
        "Make (Integromat)",
        "automation",
        "oauth",
        "https://mcp.make.com/mcp",
        "Make OAuth",
        "https://www.make.com",
        "Trigger Make scenarios across 2,000+ apps.",
    ),
    # ── Source control & dev ────────────────────────────────────────────
    _http(
        "github",
        "GitHub",
        "scm",
        "oauth",
        "https://api.githubcopilot.com/mcp/",
        "GitHub OAuth (or a fine-grained PAT in token mode)",
        "https://github.com/github/github-mcp-server",
        "GitHub's official remote MCP — repos, issues, PRs, Actions, code search.",
    ),
    _npx(
        "gitlab",
        "GitLab",
        "scm",
        "@modelcontextprotocol/server-gitlab",
        auth="env",
        env={"GITLAB_PERSONAL_ACCESS_TOKEN": "{secret}", "GITLAB_API_URL": "{prefs:api_url}"},
        credential="GitLab PAT",
        description="GitLab projects, issues, merge requests and files.",
    ),
    _http(
        "sentry",
        "Sentry",
        "observability",
        "oauth",
        "https://mcp.sentry.dev/mcp",
        "Sentry OAuth",
        "https://docs.sentry.io/product/sentry-mcp/",
        "Issues, events, traces and release health from Sentry.",
    ),
    _http(
        "vercel",
        "Vercel",
        "devops",
        "oauth",
        "https://mcp.vercel.com",
        "Vercel OAuth",
        "https://vercel.com/docs/mcp",
        "Deployments, projects, environments and logs on Vercel.",
    ),
    _http(
        "cloudflare",
        "Cloudflare",
        "devops",
        "oauth",
        "https://docs.mcp.cloudflare.com/mcp",
        "Cloudflare OAuth",
        "https://developers.cloudflare.com/agents/model-context-protocol/",
        "Workers, DNS, R2, KV and Cloudflare docs.",
    ),
    # ── Project / ticketing / productivity ──────────────────────────────
    _http(
        "atlassian",
        "Jira & Confluence (Atlassian)",
        "ticketing",
        "oauth",
        "https://mcp.atlassian.com/v1/sse",
        "Atlassian OAuth",
        "https://www.atlassian.com/platform/remote-mcp-server",
        "Jira issues/sprints and Confluence pages (Atlassian Rovo).",
    ),
    _http(
        "linear",
        "Linear",
        "ticketing",
        "oauth",
        "https://mcp.linear.app/sse",
        "Linear OAuth",
        "https://linear.app/docs/mcp",
        "Linear issues, projects, cycles and triage.",
    ),
    _http(
        "asana",
        "Asana",
        "ticketing",
        "oauth",
        "https://mcp.asana.com/sse",
        "Asana OAuth",
        "https://developers.asana.com/docs/mcp-server",
        "Asana tasks, projects and portfolios — turn chats into actions.",
    ),
    _http(
        "notion",
        "Notion",
        "docs",
        "oauth",
        "https://mcp.notion.com/mcp",
        "Notion OAuth",
        "https://developers.notion.com/docs/mcp",
        "Search, read and update Notion pages and databases.",
    ),
    _http(
        "intercom",
        "Intercom",
        "support",
        "oauth",
        "https://mcp.intercom.com/mcp",
        "Intercom OAuth",
        "https://developers.intercom.com",
        "Look up conversations, tickets and customer context in Intercom.",
    ),
    _http(
        "clickup",
        "ClickUp",
        "ticketing",
        "token",
        "https://mcp.clickup.com/mcp",
        "ClickUp API token",
        "https://clickup.com",
        "ClickUp tasks, docs and spaces.",
    ),
    # ── Design / diagramming / creative ─────────────────────────────────
    _npx(
        "drawio",
        "draw.io / diagrams",
        "design",
        "drawio-mcp-server",
        docs="https://github.com/lgazo/drawio-mcp-server",
        description="Create and edit draw.io diagrams — shapes, flows, architecture.",
    ),
    _npx(
        "figma",
        "Figma",
        "design",
        "figma-developer-mcp",
        auth="env",
        env={"FIGMA_API_KEY": "{secret}"},
        credential="Figma personal access token",
        docs="https://github.com/GLips/Figma-Context-MCP",
        description="Read Figma files, frames and design tokens; turn designs into code.",
    ),
    _http(
        "canva",
        "Canva",
        "design",
        "oauth",
        "https://mcp.canva.com/mcp",
        "Canva OAuth",
        "https://www.canva.dev/docs/apps/mcp/",
        "Search, create and edit Canva designs — posts, slides, brand assets.",
    ),
    _npx(
        "excalidraw",
        "Excalidraw",
        "design",
        "excalidraw-mcp",
        docs="https://github.com/yctimlin/mcp_excalidraw",
        description="Hand-drawn-style diagrams and whiteboards via Excalidraw.",
    ),
    # ── App builders / no-code ──────────────────────────────────────────
    _http(
        "replit",
        "Replit",
        "devops",
        "oauth",
        "https://mcp.replit.com/mcp",
        "Replit OAuth",
        "https://docs.replit.com",
        "Build, run and deploy apps on Replit.",
    ),
    _http(
        "supabase",
        "Supabase",
        "database",
        "token",
        "https://mcp.supabase.com/mcp",
        "Supabase personal access token",
        "https://supabase.com/docs/guides/getting-started/mcp",
        "Supabase Postgres, auth, storage and edge functions.",
    ),
    _http(
        "airtable",
        "Airtable",
        "database",
        "token",
        "https://mcp.airtable.com/mcp",
        "Airtable personal access token",
        "https://airtable.com",
        "Structured data — bases, tables and records in Airtable.",
    ),
    # ── Data / databases ────────────────────────────────────────────────
    _stdio(
        "postgres",
        "PostgreSQL",
        "database",
        "env",
        "npx",
        ["-y", "@modelcontextprotocol/server-postgres"],
        {"DATABASE_URL": "{secret}"},
        "Postgres connection string",
        "https://github.com/modelcontextprotocol/servers/tree/main/src/postgres",
        "Read-only SQL — schema introspection and queries over your database.",
    ),
    _stdio(
        "sqlite",
        "SQLite",
        "database",
        "env",
        "npx",
        ["-y", "@modelcontextprotocol/server-sqlite"],
        {"SQLITE_DB_PATH": "{prefs:db_path}"},
        "Path to a SQLite file (in the bridge workspace)",
        "https://github.com/modelcontextprotocol/servers/tree/main/src/sqlite",
        "Query and explore a SQLite database.",
    ),
    _npx(
        "mongodb",
        "MongoDB",
        "database",
        "mongodb-mcp-server",
        auth="env",
        env={"MDB_MCP_CONNECTION_STRING": "{secret}"},
        credential="MongoDB connection string",
        docs="https://github.com/mongodb-js/mongodb-mcp-server",
        description="Query MongoDB collections and indexes.",
    ),
    # ── Files / storage / docs ──────────────────────────────────────────
    _stdio(
        "filesystem",
        "Filesystem",
        "files",
        "none",
        "npx",
        ["-y", "@modelcontextprotocol/server-filesystem", "/workspace"],
        {},
        "none (sandboxed to the bridge workdir)",
        "https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem",
        "Read/write files in a sandboxed workspace — drafts and generated artifacts.",
    ),
    _stdio(
        "gdrive",
        "Google Drive",
        "files",
        "oauth",
        "npx",
        ["-y", "@modelcontextprotocol/server-gdrive"],
        {},
        "Google OAuth",
        "https://github.com/modelcontextprotocol/servers/tree/main/src/gdrive",
        "Search and read Google Docs, Sheets and Slides.",
    ),
    _http(
        "box",
        "Box",
        "files",
        "oauth",
        "https://mcp.box.com/mcp",
        "Box OAuth",
        "https://developer.box.com",
        "Securely search and read Box content.",
    ),
    # ── Comms / calendar / email ────────────────────────────────────────
    _stdio(
        "slack",
        "Slack",
        "messaging",
        "env",
        "npx",
        ["-y", "@modelcontextprotocol/server-slack"],
        {"SLACK_BOT_TOKEN": "{secret}", "SLACK_TEAM_ID": "{prefs:team_id}"},
        "Slack bot token (xoxb-…)",
        "https://github.com/modelcontextprotocol/servers/tree/main/src/slack",
        "Post/read Slack messages, list channels, react — agent notifications.",
    ),
    _http(
        "googleworkspace",
        "Gmail & Google Calendar",
        "productivity",
        "oauth",
        "https://mcp.google.com/workspace",
        "Google OAuth",
        "https://developers.google.com",
        "Gmail search/compose and Google Calendar events (Google Workspace).",
    ),
    # ── CRM / sales / marketing ─────────────────────────────────────────
    _http(
        "hubspot",
        "HubSpot",
        "crm",
        "oauth",
        "https://mcp.hubspot.com/anthropic",
        "HubSpot OAuth",
        "https://developers.hubspot.com/mcp",
        "HubSpot CRM — contacts, deals, companies and engagements.",
    ),
    _http(
        "stripe",
        "Stripe",
        "payments",
        "token",
        "https://mcp.stripe.com",
        "Stripe restricted API key",
        "https://docs.stripe.com/mcp",
        "Stripe customers, payments, invoices and balance.",
    ),
    _http(
        "paypal",
        "PayPal",
        "payments",
        "oauth",
        "https://mcp.paypal.com/mcp",
        "PayPal OAuth",
        "https://developer.paypal.com",
        "PayPal orders, payments and disputes.",
    ),
    # ── Search / knowledge / web ────────────────────────────────────────
    _http(
        "context7",
        "Context7 (library docs)",
        "docs",
        "token",
        "https://mcp.context7.com/mcp",
        "Context7 API key (free tier)",
        "https://context7.com",
        "Up-to-date, version-specific documentation for any library, on demand.",
    ),
    _http(
        "exa",
        "Exa Search",
        "search",
        "token",
        "https://mcp.exa.ai/mcp",
        "Exa API key",
        "https://docs.exa.ai",
        "Neural web search and content retrieval.",
    ),
    _stdio(
        "brave-search",
        "Brave Search",
        "search",
        "env",
        "npx",
        ["-y", "@modelcontextprotocol/server-brave-search"],
        {"BRAVE_API_KEY": "{secret}"},
        "Brave Search API key",
        "https://github.com/modelcontextprotocol/servers/tree/main/src/brave-search",
        "Web and local search via the Brave Search API.",
    ),
    _http(
        "huggingface",
        "Hugging Face",
        "ai",
        "token",
        "https://hf.co/mcp",
        "Hugging Face access token",
        "https://huggingface.co/settings/mcp",
        "Models, datasets, Spaces and inference on the Hugging Face Hub.",
    ),
    # ── Browser / automation / testing ──────────────────────────────────
    _npx(
        "playwright",
        "Playwright (browser)",
        "testing",
        "@playwright/mcp@latest",
        extra_args=["--headless"],
        docs="https://github.com/microsoft/playwright-mcp",
        description="Drive a real browser — navigate, fill, assert, screenshot for E2E flows.",
    ),
    _npx(
        "puppeteer",
        "Puppeteer (browser)",
        "testing",
        "@modelcontextprotocol/server-puppeteer",
        docs="https://github.com/modelcontextprotocol/servers/tree/main/src/puppeteer",
        description="Headless Chromium automation and screenshots.",
    ),
    _npx(
        "fetch",
        "Fetch (web pages)",
        "search",
        "@modelcontextprotocol/server-fetch",
        docs="https://github.com/modelcontextprotocol/servers/tree/main/src/fetch",
        description="Fetch a URL and return clean, readable content for the model.",
    ),
    # ── Observability / infra ───────────────────────────────────────────
    _http(
        "grafana",
        "Grafana",
        "observability",
        "token",
        "https://mcp.grafana.com/mcp",
        "Grafana service-account token",
        "https://github.com/grafana/mcp-grafana",
        "Dashboards, datasources, alerts and Loki/Prometheus queries.",
    ),
    _npx(
        "kubernetes",
        "Kubernetes (read)",
        "devops",
        "mcp-server-kubernetes",
        auth="none",
        docs="https://github.com/Flux159/mcp-server-kubernetes",
        description="Inspect pods, deployments, services and events in a cluster.",
    ),
]


def _doc(c: dict) -> dict:
    name = f"catalog-{c['name']}-mcp"
    native = c.get("native", "http")
    # Everything is streamable-http to the hub. stdio servers point at the
    # bridge route (filled per-connection); http servers carry the real URL.
    endpoint = c.get("endpoint", "") if native == "http" else f"{BRIDGE}{c['name']}"
    spec: dict = {
        "name": name,
        "displayName": c["display"],
        "description": c["description"],
        "endpoint": endpoint,
        "transport": "streamable-http",
        "authMode": {"token": "header", "oauth": "oauth", "none": "none", "env": "env"}[c["auth"]],
        "catalog": True,
        "connect": {
            "authKind": c["auth"],
            "native": native,
            "credential": c.get("credential", ""),
            "docs": c.get("docs", ""),
        },
    }
    if native == "stdio":
        spec["stdio"] = c["stdio"]
    return {
        "apiVersion": "registry.solo.io/v1alpha1",
        "kind": "MCPServer",
        "metadata": {
            "name": name,
            "namespace": "devai",
            "visibility": "public",
            "labels": {
                "devai.io/source": "catalog",
                "devai.io/category": c["category"],
                "mcp.devai.io/catalog": "true",
                "mcp.devai.io/auth-kind": c["auth"],
                "mcp.devai.io/native": native,
            },
        },
        "spec": spec,
    }


def main() -> None:
    # Remove stale catalog files, then write fresh.
    for old in OUT.glob("catalog-*.yaml"):
        old.unlink()
    for c in CATALOG:
        doc = _doc(c)
        path = OUT / f"{doc['metadata']['name']}.yaml"
        path.write_text(yaml.safe_dump(doc, sort_keys=False, default_flow_style=False, width=100), encoding="utf-8")
    print(f"wrote {len(CATALOG)} MCP catalog templates -> {OUT}")


if __name__ == "__main__":
    main()
