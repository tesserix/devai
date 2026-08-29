import assert from "node:assert/strict";
import test from "node:test";

import nextConfig from "../../next.config.ts";

test("routes SRE Studio requests to the native API before the legacy catch-all", async () => {
  if (typeof nextConfig.rewrites !== "function") {
    assert.fail("Next.js rewrites are not configured");
  }

  const configured = await nextConfig.rewrites();
  if (!Array.isArray(configured)) {
    assert.fail("Expected rewrites to be a flat ordered list");
  }

  const sreStudioIndex = configured.findIndex(
    (route) => route.source === "/api/sre-studio/:path*",
  );
  const legacyIndex = configured.findIndex(
    (route) => route.source === "/api/:path*",
  );

  assert.notEqual(sreStudioIndex, -1, "SRE Studio needs a native API pass-through");
  assert.notEqual(legacyIndex, -1, "legacy API catch-all must remain configured");
  assert.ok(sreStudioIndex < legacyIndex, "SRE Studio pass-through must precede the catch-all");
  assert.match(
    configured[sreStudioIndex].destination,
    /\/api\/sre-studio\/:path\*$/,
  );
});
