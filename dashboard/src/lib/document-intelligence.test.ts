import assert from "node:assert/strict";
import test from "node:test";

import { parseDocumentJobState, parseDocumentUploadState } from "./document-intelligence.ts";

test("accepts the minimal opaque upload lifecycle response", () => {
  assert.deepEqual(
    parseDocumentUploadState({ upload_id: "upl_01TEST", status: "inspecting" }),
    { upload_id: "upl_01TEST", status: "inspecting" },
  );
});

test("rejects upload lifecycle response fields that could expose document data", () => {
  assert.throws(
    () => parseDocumentUploadState({ upload_id: "upl_01TEST", status: "accepted", object_uri: "gs://private" }),
    /invalid document upload response/,
  );
});

test("rejects unknown lifecycle statuses", () => {
  assert.throws(
    () => parseDocumentUploadState({ upload_id: "upl_01TEST", status: "completed" }),
    /invalid document upload response/,
  );
});

test("rejects a status response for a different upload", () => {
  assert.throws(
    () => parseDocumentUploadState({ upload_id: "upl_other", status: "accepted" }, "upl_01TEST"),
    /invalid document upload response/,
  );
});

test("accepts the minimal opaque job lifecycle response", () => {
  assert.deepEqual(
    parseDocumentJobState({ job_id: "job_01TEST", status: "processing" }),
    { job_id: "job_01TEST", status: "processing" },
  );
});

test("rejects job lifecycle response fields that could expose document data", () => {
  assert.throws(
    () => parseDocumentJobState({ job_id: "job_01TEST", status: "completed", text: "private" }),
    /invalid document job response/,
  );
});
