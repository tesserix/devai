import type { NextConfig } from "next";

// ── Security response headers (DASH-3) ──────────────────────────────────────
// The SRE dashboard renders chat markdown via dangerouslySetInnerHTML (now
// DOMPurify-sanitized, DASH-4), so a CSP is the containment layer if the
// renderer ever regresses, and frame-ancestors blocks clickjacking of the
// session-bearing UI. Mirrors the ALM dashboard.
//
// script-src uses 'unsafe-inline' and NO hash: Next.js App Router emits inline
// hydration/route-data scripts (self.__next_f.push) plus our inline theme-init
// script. A hash/nonce in script-src makes browsers IGNORE 'unsafe-inline' —
// which previously blocked Next's inline scripts and left the app a blank
// screen. Hardening to a per-request middleware nonce (then dropping
// 'unsafe-inline') is the planned next step.

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
  // 'unsafe-inline' for Next's inline runtime + the theme script (see note
  // above). No hash/nonce here or it would disable 'unsafe-inline'. 'unsafe-eval'
  // is NOT allowed. apis.google.com + gstatic are the Firebase/Google sign-in
  // loader scripts (GIP popup), without which sign-in CSP-fails as auth/internal-error.
  "script-src 'self' 'unsafe-inline' https://apis.google.com https://www.gstatic.com",
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data: blob: https:",
  "font-src 'self' data:",
  `connect-src ${CONNECT_SRC}`,
  // Frames only the GIP Google sign-in OAuth helper (popup/iframe).
  "frame-src 'self' https://*.firebaseapp.com https://apis.google.com https://accounts.google.com",
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
