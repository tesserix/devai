import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

// Paths that don't require authentication
const PUBLIC_PATHS = [
  "/bff/",
  "/auth/",
  "/_next/",
  "/favicon.ico",
];

export function middleware(request: NextRequest) {
  const { pathname } = request.nextUrl;

  // Skip auth for public paths
  if (PUBLIC_PATHS.some((p) => pathname.startsWith(p))) {
    return NextResponse.next();
  }

  // Check for session cookie (set by auth-bff)
  const session = request.cookies.get("devai_session");
  if (session) {
    return NextResponse.next();
  }

  // No session — redirect to auth-bff login
  const loginUrl = new URL("/auth/login", request.url);
  loginUrl.searchParams.set("redirect_uri", request.url);
  return NextResponse.redirect(loginUrl);
}

export const config = {
  matcher: [
    // Match all paths except static files
    "/((?!_next/static|_next/image|favicon.ico).*)",
  ],
};
