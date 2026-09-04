import assert from "node:assert/strict";
import test from "node:test";

import nextConfig from "../../next.config.ts";

test("routes document-intelligence requests to the native API before the legacy catch-all", async () => {
  if (typeof nextConfig.rewrites !== "function") {
    assert.fail("Next.js rewrites are not configured");
  }

  const configured = await nextConfig.rewrites();
  if (!Array.isArray(configured)) {
    assert.fail("Expected rewrites to be a flat ordered list");
  }

  const documentIntelligenceIndex = configured.findIndex(
    (route) => route.source === "/api/document-intelligence/:path*",
  );
  const legacyIndex = configured.findIndex(
    (route) => route.source === "/api/:path*",
  );

  assert.notEqual(documentIntelligenceIndex, -1, "document sandbox needs a native API pass-through");
  assert.notEqual(legacyIndex, -1, "legacy API catch-all must remain configured");
  assert.ok(documentIntelligenceIndex < legacyIndex, "document pass-through must precede the catch-all");
  assert.match(
    configured[documentIntelligenceIndex].destination,
    /\/api\/document-intelligence\/:path\*$/,
  );
});
