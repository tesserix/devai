import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { NEW_RUN_HREF, newRunHref, runDetailHref } from "./run-entry.ts";

test("all new-run links use the shared workflow dialog route", () => {
  assert.equal(NEW_RUN_HREF, "/workflows?action=new");
  assert.equal(newRunHref(), NEW_RUN_HREF);
  assert.equal(
    newRunHref("tesserix/repo with spaces"),
    "/workflows?action=new&repo=tesserix%2Frepo+with+spaces",
  );
  assert.equal(runDetailHref("devai/a b"), "/runs/devai%2Fa%20b");
});

test("user-facing describe-task entry points no longer navigate to compose", () => {
  const entryPoints = [
    "../app/page.tsx",
    "../components/mission-control-nav.tsx",
    "../components/mission-control-shell.tsx",
    "../components/command-palette.tsx",
    "./help-content.ts",
  ];

  for (const relativePath of entryPoints) {
    const source = readFileSync(new URL(relativePath, import.meta.url), "utf8");
    assert.doesNotMatch(source, /["'`]\/compose(?:\?|["'`])/u, relativePath);
  }
});

