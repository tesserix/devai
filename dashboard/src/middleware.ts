import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

const PUBLIC_PATHS = [
  "/login",
  "/bff/",
  "/auth/",
  "/api/health",
  "/_next/",
  "/favicon.ico",
];

// /api/* is proxied straight to devai-api, which trusts the session cookie the
// proxy forwards. Letting it through unauthenticated exposed every API family
// on the public host, so an unauthenticated call is refused here — with a 401
// rather than the login redirect, which an XHR would read as a 200 of HTML.
function isApi(pathname: string): boolean {
  return pathname.startsWith("/api/") || pathname.startsWith("/chat/");
}

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

  if (isApi(pathname)) {
    return noStore(
      NextResponse.json({ detail: "authentication required" }, { status: 401 }),
    );
  }

  return noStore(NextResponse.redirect(new URL("/login", request.url)));
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
