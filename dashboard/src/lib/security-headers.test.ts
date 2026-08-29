import assert from "node:assert/strict";
import test from "node:test";

import nextConfig from "../../next.config.ts";

test("CSP permits the validated IPv4 loopback CLI callback", async () => {
  const rules = await nextConfig.headers?.();
  const csp = rules
    ?.flatMap((rule) => rule.headers)
    .find((header) => header.key === "Content-Security-Policy")?.value;

  assert.ok(csp);
  assert.match(csp, /connect-src [^;]*http:\/\/127\.0\.0\.1:\*/);
  assert.doesNotMatch(csp, /http:\/\/localhost/);
  assert.doesNotMatch(csp, /http:\/\/\[::1\]/);
});
