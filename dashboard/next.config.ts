import type { NextConfig } from "next";

// In K8s, the dashboard proxies API calls to devai-api via internal
// service DNS. Locally, it falls back to localhost:8080. The mapping
// is split because devai-api mounts different route families on
// different prefixes:
//
//   /dashboard/api/*        legacy dashboard endpoints (runs, agents, …)
//   /chat/api/*             chat agent (REST + SSE)
//   /api/registry/*         aregistry catalogue surface
//   /api/agentic/*          agentic control-plane status + LLM probe
//   /api/pipeline/*         Fiber-style blueprint runtime
//   /api/specializations/*  YAML role catalogue
//   /webhook/*              SCM webhooks
//
// Order matters: the most-specific rewrite has to come first because
// Next.js applies them in order until one matches. The final
// catch-all under /api/* still proxies legacy dashboard calls
// (/api/runs, /api/agents, etc.) into /dashboard/api/* for the
// original page.tsx. Before this fix the catch-all swallowed
// /api/registry/* and rewrote it to /dashboard/api/registry/* which
// doesn't exist on devai-api — hence the dashboard's status bar
// showed 'offline'.
const API_INTERNAL_URL =
  process.env.DEVAI_API_INTERNAL_URL || "http://devai-api.devai.svc.cluster.local:8080";

const nextConfig: NextConfig = {
  output: "standalone",
  async rewrites() {
    // LOCAL ONLY: when DEVAI_AUTH_MODE=local_db, proxy /auth/* to devai-api's
    // username/password endpoints. In prod this env is unset, so /auth/* is
    // left for the auth-bff (GIP) to handle as before.
    const localAuthProxy =
      process.env.DEVAI_AUTH_MODE === "local_db"
        ? [{ source: "/auth/:path*", destination: `${API_INTERNAL_URL}/auth/:path*` }]
        : [];
    return [
      ...localAuthProxy,
      {
        source: "/api/chat/:path*",
        destination: `${API_INTERNAL_URL}/chat/api/:path*`,
      },
      // Pass-throughs — these families live at /api/* on devai-api
      // directly (NOT under /dashboard/api/*). Must be declared
      // BEFORE the legacy catch-all.
      {
        source: "/api/registry/:path*",
        destination: `${API_INTERNAL_URL}/api/registry/:path*`,
      },
      {
        source: "/api/agentic/:path*",
        destination: `${API_INTERNAL_URL}/api/agentic/:path*`,
      },
      {
        source: "/api/pipeline/:path*",
        destination: `${API_INTERNAL_URL}/api/pipeline/:path*`,
      },
      {
        source: "/api/specializations/:path*",
        destination: `${API_INTERNAL_URL}/api/specializations/:path*`,
      },
      {
        source: "/api/scm/:path*",
        destination: `${API_INTERNAL_URL}/api/scm/:path*`,
      },
      // Authoring (create/list/delete agents, skills, tools, blueprints) —
      // mounted at /api/authoring/* on devai-api, NOT the legacy prefix.
      {
        source: "/api/authoring/:path*",
        destination: `${API_INTERNAL_URL}/api/authoring/:path*`,
      },
      // Settings (per-user/tenant connectors + secrets) — /api/settings/*.
      {
        source: "/api/settings/:path*",
        destination: `${API_INTERNAL_URL}/api/settings/:path*`,
      },
      // Catalog (built-in tools picker), Teams/crews (composer), per-run
      // repo viewer — all live at /api/* on devai-api directly.
      {
        source: "/api/catalog/:path*",
        destination: `${API_INTERNAL_URL}/api/catalog/:path*`,
      },
      {
        source: "/api/teams/:path*",
        destination: `${API_INTERNAL_URL}/api/teams/:path*`,
      },
      {
        source: "/api/runs/:path*",
        destination: `${API_INTERNAL_URL}/api/runs/:path*`,
      },
      // Legacy catch-all — original dashboard endpoints sit under
      // /dashboard/api/* on devai-api. Keep this last so it doesn't
      // shadow the more-specific rewrites above.
      {
        source: "/api/:path*",
        destination: `${API_INTERNAL_URL}/dashboard/api/:path*`,
      },
    ];
  },
};

export default nextConfig;
