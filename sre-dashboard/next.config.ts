import type { NextConfig } from "next";
import { createHash } from "crypto";
import { THEME_INIT_SCRIPT } from "./src/lib/theme-script";

// ── Security response headers (DASH-3) ──────────────────────────────────────
// The SRE dashboard set no headers() before. It renders chat markdown via
// dangerouslySetInnerHTML (now DOMPurify-sanitized, DASH-4), so a strict CSP is
// the containment layer if the renderer ever regresses, and frame-ancestors
// blocks clickjacking of the session-bearing UI. Mirrors the ALM dashboard.
//
// script-src: we pin the SHA-256 hash of the inline theme-init script so it is
// explicitly allowed. Next.js (App Router, no middleware nonce here) also emits
// inline hydration/route-data scripts; without a per-request nonce those need
// 'unsafe-inline'. Browsers ignore 'unsafe-inline' once a hash/nonce is present,
// so to keep the app working we rely on 'unsafe-inline' for the Next runtime AND
// list the theme hash for documentation/forward-compat. The harder lock-down
// (drop 'unsafe-inline', add a middleware nonce) is the planned next step.
const THEME_SCRIPT_HASH = `'sha256-${createHash("sha256")
  .update(THEME_INIT_SCRIPT, "utf8")
  .digest("base64")}'`;

// Hosts the app legitimately talks to. Firebase + Google Identity for sign-in
// (the dashboard fetches its Firebase config from the BFF and signs in via GIP).
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

const CSP = [
  "default-src 'self'",
  // 'unsafe-inline' kept for Next's inline runtime (see note above); theme hash
  // listed for the eventual nonce-based hardening. 'unsafe-eval' is NOT allowed.
  `script-src 'self' 'unsafe-inline' ${THEME_SCRIPT_HASH}`,
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data: blob: https:",
  "font-src 'self' data:",
  `connect-src ${CONNECT_SRC}`,
  // The SRE dashboard frames nothing; deny all framing both directions.
  "frame-src 'none'",
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

// In K8s, the SRE dashboard calls the SRE API via internal service DNS.
// Locally, it falls back to localhost:8090.
//
// NOTE (DASH-11): the dashboard reaches the API ONLY through this server-side
// rewrite proxy — it never reads any browser-exposed base URL. The
// NEXT_PUBLIC_API_URL value historically set in the devai-sre-dashboard chart
// values is misleading dead config (nothing in this app reads it) and should be
// removed from the chart; it is intentionally NOT referenced here.
const SRE_API_INTERNAL_URL =
  process.env.DEVAI_SRE_API_INTERNAL_URL || "http://devai-sre.devai.svc.cluster.local:8090";

const nextConfig: NextConfig = {
  output: "standalone",
  async rewrites() {
    return [
      { source: "/api/:path*", destination: `${SRE_API_INTERNAL_URL}/api/:path*` },
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
