import assert from "node:assert/strict";
import test from "node:test";

import {
  cancelSandboxDocumentJob,
  canCancelDocumentJob,
  documentJobProgress,
  isDocumentJobActive,
  documentUploadFailureMessage,
  parseDocumentJobResult,
  parseDocumentJobState,
  parseDocumentSandboxSession,
  parseDocumentUploadState,
  resolveDocumentSandboxSessionRestore,
} from "./document-intelligence.ts";

test("restores only opaque sandbox identifiers from a browser session", () => {
  assert.deepEqual(
    parseDocumentSandboxSession('{"upload_id":"upl_01TEST","job_id":"job_01TEST"}'),
    { upload_id: "upl_01TEST", job_id: "job_01TEST" },
  );
});

test("rejects browser session data that includes document content or invalid identifiers", () => {
  assert.equal(parseDocumentSandboxSession('{"upload_id":"upl_01TEST","text":"private"}'), null);
  assert.equal(parseDocumentSandboxSession('{"job_id":"not-a-job"}'), null);
  assert.equal(parseDocumentSandboxSession("not json"), null);
});

test("keeps an authorized OCR job when its upload record can no longer be restored", () => {
  assert.deepEqual(
    resolveDocumentSandboxSessionRestore(
      { status: "rejected", reason: new Error("upload expired") },
      { status: "fulfilled", value: { job_id: "job_01TEST", status: "completed" } },
    ),
    {
      upload: null,
      job: { job_id: "job_01TEST", status: "completed" },
      uploadUnavailable: true,
      jobUnavailable: false,
    },
  );
});

test("allows cancellation only while an OCR job is non-terminal", () => {
  assert.equal(canCancelDocumentJob("accepted"), true);
  assert.equal(canCancelDocumentJob("processing"), true);
  assert.equal(canCancelDocumentJob("cancelling"), false);
  assert.equal(canCancelDocumentJob("completed"), false);
  assert.equal(canCancelDocumentJob("cancelled"), false);
  assert.equal(isDocumentJobActive("cancelling"), true);
  assert.equal(isDocumentJobActive("cancelled"), false);
});

test("cancels a job through the scoped sandbox endpoint", async () => {
  const originalFetch = globalThis.fetch;
  let method = "";
  let path = "";
  globalThis.fetch = async (input, init) => {
    method = init?.method ?? "GET";
    path = String(input);
    return new Response(JSON.stringify({ job_id: "job_01TEST", status: "cancelling" }), { status: 200 });
  };
  try {
    assert.deepEqual(await cancelSandboxDocumentJob("job_01TEST"), { job_id: "job_01TEST", status: "cancelling" });
    assert.equal(method, "POST");
    assert.equal(path, "/api/document-intelligence/jobs/job_01TEST/cancel");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("derives progress only from the durable OCR lifecycle", () => {
  assert.deepEqual(documentJobProgress("accepted"), {
    stage: "queued",
    message: "Queued for durable OCR execution.",
  });
  assert.deepEqual(documentJobProgress("processing"), {
    stage: "extracting",
    message: "Extracting text, layout, and evidence.",
  });
  assert.deepEqual(documentJobProgress("completed"), {
    stage: "complete",
    message: "OCR completed. Result details are ready.",
  });
});

test("maps upload failures to safe actionable guidance", () => {
  assert.equal(documentUploadFailureMessage(413), "This file is larger than the sandbox's 100 MB upload limit.");
  assert.equal(
    documentUploadFailureMessage(422),
    "This file could not be accepted. Use a valid PDF, PNG, JPEG, TIFF, or WebP and try again.",
  );
  assert.equal(
    documentUploadFailureMessage(503),
    "The upload service is temporarily unavailable. Try again shortly.",
  );
});

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

test("accepts a bounded document result diagnostics response", () => {
  assert.deepEqual(
    parseDocumentJobResult({
      job_id: "job_01TEST",
      summary: { page_count: 1, observation_count: 4, field_count: 2, table_count: 0, citation_count: 2 },
      confidence: { input_quality: 0.9, ocr: 0.91, classification: 0.92, extraction: 0.93, validation: 1, overall: 0.92 },
      warnings: ["low_quality_scan"],
      validation_failures: [{ code: "total_mismatch", severity: "warning" }],
      provider: "tesserix",
      model_version: "ocr-1",
      processing_profile_version: "printed-en-v1",
      duration_ms: 42,
      cost: { currency: "AUD", decimal: "0.0012" },
      fields: [{ name: "total", value: { decimal: "12.50" }, confidence: 0.97, pages: [1] }],
      text_preview: "Total 12.50",
      text_truncated: false,
    }, "job_01TEST"),
    {
      job_id: "job_01TEST",
      summary: { page_count: 1, observation_count: 4, field_count: 2, table_count: 0, citation_count: 2 },
      confidence: { input_quality: 0.9, ocr: 0.91, classification: 0.92, extraction: 0.93, validation: 1, overall: 0.92 },
      warnings: ["low_quality_scan"],
      validation_failures: [{ code: "total_mismatch", severity: "warning" }],
      provider: "tesserix",
      model_version: "ocr-1",
      processing_profile_version: "printed-en-v1",
      duration_ms: 42,
      cost: { currency: "AUD", decimal: "0.0012" },
      fields: [{ name: "total", value: { decimal: "12.50" }, confidence: 0.97, pages: [1] }],
      text_preview: "Total 12.50",
      text_truncated: false,
    },
  );
});

test("rejects unbounded or unexpected document result fields", () => {
  assert.throws(
    () => parseDocumentJobResult({ job_id: "job_01TEST", object_uri: "gs://private" }, "job_01TEST"),
    /invalid document result response/,
  );
});
