import assert from "node:assert/strict";
import test from "node:test";

import {
  documentUploadFailureMessage,
  parseDocumentJobResult,
  parseDocumentJobState,
  parseDocumentUploadState,
} from "./document-intelligence.ts";

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
