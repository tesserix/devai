import { strict as assert } from "node:assert";
import { test } from "node:test";
import { editorDocument, fieldsFor, starter } from "./registry-schemas.ts";

test("user-authored artifacts are private by default and cannot select broader visibility", () => {
  const manifest = starter("Agent");
  const metadata = manifest.metadata as Record<string, unknown>;
  const visibility = fieldsFor("Agent").find((field) => field.path === "metadata.visibility");

  assert.equal(metadata.visibility, "private");
  assert.deepEqual(visibility?.options, ["private"]);
  assert.match(visibility?.help ?? "", /private/i);
});

test("editing starts from an independent copy of the published manifest", () => {
  const published = starter("Agent");
  const publishedSpec = published.spec as Record<string, unknown>;
  publishedSpec.systemPrompt = "Original prompt";

  const editing = editorDocument("Agent", published);
  const editingSpec = editing.spec as Record<string, unknown>;
  editingSpec.systemPrompt = "New version";

  assert.equal(publishedSpec.systemPrompt, "Original prompt");
  assert.equal(editingSpec.systemPrompt, "New version");
});
