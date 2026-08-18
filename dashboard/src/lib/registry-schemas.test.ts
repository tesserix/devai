import { strict as assert } from "node:assert";
import { test } from "node:test";
import { fieldsFor, starter } from "./registry-schemas.ts";

test("user-authored artifacts are private by default and cannot select broader visibility", () => {
  const manifest = starter("Agent");
  const metadata = manifest.metadata as Record<string, unknown>;
  const visibility = fieldsFor("Agent").find((field) => field.path === "metadata.visibility");

  assert.equal(metadata.visibility, "private");
  assert.deepEqual(visibility?.options, ["private"]);
  assert.match(visibility?.help ?? "", /private/i);
});
