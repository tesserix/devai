/**
 * safeReturn — validate an untrusted ?return_to= value (DASH-1).
 *
 * Returns a destination that is safe to redirect to after sign-in, or "/" if
 * the input is missing or hostile. An unvalidated return_to is a classic
 * post-auth open-redirect / phishing primitive.
 *
 * Accepted:
 *   - a same-site relative path: starts with "/" but NOT "//" (protocol-relative)
 *     and not "/\" (browsers treat "/\evil.com" as protocol-relative).
 *   - an absolute http(s) URL whose hostname is exactly "tesserix.app" or a
 *     subdomain of it ("*.tesserix.app") — the session cookie is on
 *     .tesserix.app so those are legitimately in-family.
 * Everything else (other hosts, javascript:, data:, mailto:, malformed) → "/".
 *
 * Lives in lib (not the page) so it can be imported by both dashboards and
 * unit-tested, and so the login page only exports the Next route surface.
 */
export function safeReturn(raw: string | null | undefined): string {
  if (!raw) return "/";
  const value = raw.trim();
  if (!value) return "/";

  // Relative path — must be rooted, and must not be protocol-relative ("//host")
  // or backslash-tricked ("/\\host"), both of which navigate cross-origin.
  if (value.startsWith("/")) {
    if (value.startsWith("//") || value.startsWith("/\\")) return "/";
    return value;
  }

  // Absolute URL — only allow the tesserix.app family over http(s).
  try {
    const url = new URL(value);
    if (url.protocol !== "https:" && url.protocol !== "http:") return "/";
    const host = url.hostname.toLowerCase();
    if (host === "tesserix.app" || host.endsWith(".tesserix.app")) {
      return url.toString();
    }
  } catch {
    /* not a parseable absolute URL — fall through */
  }
  return "/";
}
