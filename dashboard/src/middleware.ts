import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

const PUBLIC_PATHS = [
  "/login",
  "/bff/",
  "/auth/",
  "/api/",
  "/chat/",
  "/_next/",
  "/favicon.ico",
];

// Dashboard pages are dynamic + auth-gated, so the CDN must never cache the
// HTML. Next statically prerendered some routes with `s-maxage=31536000`
// (1 year), so when response headers changed (e.g. the CSP gaining the Google
// sign-in hosts) Cloudflare kept serving the STALE headers and a browser
// refresh couldn't bust the edge — which broke sign-in. no-store keeps every
// page fresh. Immutable `_next/static` assets are excluded by the matcher.
function noStore(res: NextResponse): NextResponse {
  res.headers.set("Cache-Control", "no-store, must-revalidate");
  return res;
}

export function middleware(request: NextRequest) {
  const { pathname } = request.nextUrl;

  if (PUBLIC_PATHS.some((p) => pathname.startsWith(p))) {
    return noStore(NextResponse.next());
  }

  const session = request.cookies.get("devai_session");
  if (session) {
    return noStore(NextResponse.next());
  }

  return noStore(NextResponse.redirect(new URL("/login", request.url)));
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
