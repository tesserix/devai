import type { NextConfig } from "next";
import { createHash } from "crypto";
import { THEME_INIT_SCRIPT } from "./src/lib/theme-script";

// ── Security response headers (DASH-3) ──────────────────────────────────────
// Neither dashboard set any headers(). Both render chat markdown via
// dangerouslySetInnerHTML, so a strict CSP is the containment layer if the
// (now sanitized) renderer ever regresses, and frame-ancestors blocks
// clickjacking of the session-bearing UI.
//
// script-src: we pin the SHA-256 hash of the inline theme-init script so it is
// explicitly allowed. Next.js (App Router, no middleware here) also emits inline
// hydration/route-data scripts; without a per-request nonce those need
// 'unsafe-inline'. Browsers ignore 'unsafe-inline' once a hash/nonce is present,
// so to keep the app working we currently rely on 'unsafe-inline' for the Next
// runtime AND list the theme hash for documentation/forward-compat. The harder
// lock-down (drop 'unsafe-inline', add a middleware nonce) is the planned next
// step — tracked alongside the auth hardening.
const THEME_SCRIPT_HASH = `'sha256-${createHash("sha256")
  .update(THEME_INIT_SCRIPT, "utf8")
  .digest("base64")}'`;

// Hosts the app legitimately talks to / frames. Firebase + Google Identity for
// sign-in; the preview hosts for the run Preview iframe.
const CONNECT_SRC = [
  "'self'",
  "https://*.googleapis.com",
  "https://*.firebaseio.com",
  "https://*.firebaseapp.com",
  "https://identitytoolkit.googleapis.com",
  "https://securetoken.googleapis.com",
  "https://*.tesserix.app",
  "wss://*.tesserix.app",
].join(" ");

const FRAME_SRC = ["'self'", "https://*.tesserix.app"].join(" ");

const CSP = [
  "default-src 'self'",
  // 'unsafe-inline' kept for Next's inline runtime (see note above); theme hash
  // listed for the eventual nonce-based hardening. 'unsafe-eval' is NOT allowed.
  `script-src 'self' 'unsafe-inline' ${THEME_SCRIPT_HASH}`,
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data: blob: https:",
  "font-src 'self' data:",
  `connect-src ${CONNECT_SRC}`,
  `frame-src ${FRAME_SRC}`,
  "frame-ancestors 'none'",
  "object-src 'none'",
  "base-uri 'self'",
  "form-action 'self'",
].join("; ");

const SECURITY_HEADERS = [
  { key: "Content-Security-Policy", value: CSP },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  { key: "X-Frame-Options", value: "DENY" },
];

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
  async headers() {
    return [
      {
        // Apply the security headers to every route.
        source: "/:path*",
        headers: SECURITY_HEADERS,
      },
    ];
  },
};

export default nextConfig;
