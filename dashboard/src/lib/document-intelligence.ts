export const DOCUMENT_UPLOAD_STATUSES = [
  "reserved",
  "uploaded",
  "inspecting",
  "accepted",
  "rejected",
  "expired",
] as const;

export const DOCUMENT_JOB_STATUSES = [
  "accepted",
  "inspecting",
  "processing",
  "validating",
  "cancelling",
  "cancelled",
  "rejected",
  "partial",
  "review_required",
  "completed",
] as const;

export type DocumentUploadStatus = (typeof DOCUMENT_UPLOAD_STATUSES)[number];

export type DocumentUploadState = {
  upload_id: string;
  status: DocumentUploadStatus;
};

export type DocumentJobState = {
  job_id: string;
  status: (typeof DOCUMENT_JOB_STATUSES)[number];
};

export type DocumentSandboxSession = {
  upload_id?: string;
  job_id?: string;
};

export type DocumentSandboxSessionRestore = {
  upload: DocumentUploadState | null;
  job: DocumentJobState | null;
  uploadUnavailable: boolean;
  jobUnavailable: boolean;
};

export type DocumentJobProgress = {
  stage: "queued" | "preparing" | "extracting" | "validating" | "complete" | "attention" | "cancelled";
  message: string;
};

type JsonValue = null | boolean | number | string | JsonValue[] | { [key: string]: JsonValue };

type ConfidenceDimensions = {
  input_quality: number;
  ocr: number;
  classification: number;
  extraction: number;
  validation: number;
  overall: number;
};

export type DocumentJobResult = {
  job_id: string;
  summary: {
    page_count: number;
    observation_count: number;
    field_count: number;
    table_count: number;
    citation_count: number;
  };
  confidence: ConfidenceDimensions | null;
  warnings: string[];
  validation_failures: { code: string; severity: "warning" | "error" }[];
  provider: string | null;
  model_version: string | null;
  processing_profile_version: string | null;
  duration_ms: number | null;
  cost: { currency: string; decimal: string } | null;
  fields: { name: string; value: JsonValue; confidence: number; pages: number[] }[];
  text_preview: string;
  text_truncated: boolean;
};

export class DocumentUploadError extends Error {
  readonly status: number;

  constructor(status: number) {
    super(documentUploadFailureMessage(status));
    this.name = "DocumentUploadError";
    this.status = status;
  }
}

export function documentUploadFailureMessage(status: number): string {
  if (status === 413) return "This file is larger than the sandbox's 100 MB upload limit.";
  if (status === 401 || status === 403) return "Your sandbox session has expired. Refresh the page and sign in again.";
  if (status === 415 || status === 422) {
    return "This file could not be accepted. Use a valid PDF, PNG, JPEG, TIFF, or WebP and try again.";
  }
  if (status === 429) return "Sandbox upload capacity is busy. Wait a moment, then try again.";
  return "The upload service is temporarily unavailable. Try again shortly.";
}

export function documentJobProgress(status: DocumentJobState["status"]): DocumentJobProgress {
  switch (status) {
    case "accepted":
      return { stage: "queued", message: "Queued for durable OCR execution." };
    case "inspecting":
      return { stage: "preparing", message: "Preparing the document for OCR." };
    case "processing":
      return { stage: "extracting", message: "Extracting text, layout, and evidence." };
    case "validating":
      return { stage: "validating", message: "Validating extracted evidence." };
    case "completed":
      return { stage: "complete", message: "OCR completed. Result details are ready." };
    case "partial":
    case "review_required":
      return { stage: "attention", message: "OCR finished with items requiring attention." };
    case "cancelling":
      return { stage: "cancelled", message: "Cancelling durable OCR execution." };
    case "cancelled":
      return { stage: "cancelled", message: "OCR was cancelled." };
    case "rejected":
      return { stage: "attention", message: "OCR could not process this document." };
  }
}

export function canCancelDocumentJob(status: DocumentJobState["status"]): boolean {
  return !["cancelling", "cancelled", "rejected", "partial", "review_required", "completed"].includes(status);
}

export function isDocumentJobActive(status: DocumentJobState["status"]): boolean {
  return !["cancelled", "rejected", "partial", "review_required", "completed"].includes(status);
}

function isDocumentUploadStatus(value: unknown): value is DocumentUploadStatus {
  return typeof value === "string" && DOCUMENT_UPLOAD_STATUSES.includes(value as DocumentUploadStatus);
}

function isDocumentJobStatus(value: unknown): value is DocumentJobState["status"] {
  return typeof value === "string" && DOCUMENT_JOB_STATUSES.includes(value as DocumentJobState["status"]);
}

function isOpaqueSandboxIdentifier(value: unknown, prefix: "upl_" | "job_"): value is string {
  return typeof value === "string" && value.startsWith(prefix) && value.length <= 256;
}

export function parseDocumentSandboxSession(value: string | null): DocumentSandboxSession | null {
  if (value === null) return null;

  try {
    const session: unknown = JSON.parse(value);
    if (!session || typeof session !== "object" || Array.isArray(session)) return null;
    const fields = session as Record<string, unknown>;
    const keys = Object.keys(fields);
    if (keys.length === 0 || keys.length > 2 || !keys.every((key) => key === "upload_id" || key === "job_id")) return null;
    if ("upload_id" in fields && !isOpaqueSandboxIdentifier(fields.upload_id, "upl_")) return null;
    if ("job_id" in fields && !isOpaqueSandboxIdentifier(fields.job_id, "job_")) return null;
    return {
      ...(typeof fields.upload_id === "string" ? { upload_id: fields.upload_id } : {}),
      ...(typeof fields.job_id === "string" ? { job_id: fields.job_id } : {}),
    };
  } catch {
    return null;
  }
}

export function resolveDocumentSandboxSessionRestore(
  upload: PromiseSettledResult<DocumentUploadState | null>,
  job: PromiseSettledResult<DocumentJobState | null>,
): DocumentSandboxSessionRestore {
  return {
    upload: upload.status === "fulfilled" ? upload.value : null,
    job: job.status === "fulfilled" ? job.value : null,
    uploadUnavailable: upload.status === "rejected",
    jobUnavailable: job.status === "rejected",
  };
}

export function serializeDocumentSandboxSession(session: DocumentSandboxSession): string {
  return JSON.stringify(session);
}

export function parseDocumentUploadState(value: unknown, expectedUploadId?: string): DocumentUploadState {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("invalid document upload response");
  }

  const response = value as Record<string, unknown>;
  const keys = Object.keys(response);
  if (
    keys.length !== 2 ||
    !keys.includes("upload_id") ||
    !keys.includes("status") ||
    typeof response.upload_id !== "string" ||
    !response.upload_id.startsWith("upl_") ||
    response.upload_id.length > 256 ||
    !isDocumentUploadStatus(response.status) ||
    (expectedUploadId !== undefined && response.upload_id !== expectedUploadId)
  ) {
    throw new Error("invalid document upload response");
  }

  return { upload_id: response.upload_id, status: response.status };
}

export function parseDocumentJobState(value: unknown, expectedJobId?: string): DocumentJobState {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("invalid document job response");
  }

  const response = value as Record<string, unknown>;
  const keys = Object.keys(response);
  if (
    keys.length !== 2 ||
    !keys.includes("job_id") ||
    !keys.includes("status") ||
    typeof response.job_id !== "string" ||
    !response.job_id.startsWith("job_") ||
    response.job_id.length > 256 ||
    !isDocumentJobStatus(response.status) ||
    (expectedJobId !== undefined && response.job_id !== expectedJobId)
  ) {
    throw new Error("invalid document job response");
  }

  return { job_id: response.job_id, status: response.status };
}

function isScore(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 1;
}

function isCount(value: unknown): value is number {
  return Number.isInteger(value) && typeof value === "number" && value >= 0;
}

function isJsonValue(value: unknown, depth = 0): value is JsonValue {
  if (value === null || typeof value === "boolean" || typeof value === "string") return true;
  if (typeof value === "number") return Number.isFinite(value);
  if (depth >= 10 || Array.isArray(value) && value.length > 100) return false;
  if (Array.isArray(value)) return value.every((item) => isJsonValue(item, depth + 1));
  return typeof value === "object" && Object.keys(value).length <= 100 && Object.values(value).every((item) => isJsonValue(item, depth + 1));
}

function isPageNumbers(value: unknown): value is number[] {
  return Array.isArray(value) && value.every(isCount);
}

export function parseDocumentJobResult(value: unknown, expectedJobId?: string): DocumentJobResult {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("invalid document result response");
  const response = value as Record<string, unknown>;
  const expectedKeys = [
    "job_id", "summary", "confidence", "warnings", "validation_failures", "provider", "model_version",
    "processing_profile_version", "duration_ms", "cost", "fields", "text_preview", "text_truncated",
  ];
  if (Object.keys(response).length !== expectedKeys.length || !expectedKeys.every((key) => key in response)
    || typeof response.job_id !== "string" || !response.job_id.startsWith("job_") || response.job_id.length > 256
    || expectedJobId !== undefined && response.job_id !== expectedJobId || typeof response.text_preview !== "string"
    || response.text_preview.length > 10_000 || typeof response.text_truncated !== "boolean") {
    throw new Error("invalid document result response");
  }
  const summary = response.summary;
  if (!summary || typeof summary !== "object" || Array.isArray(summary)) throw new Error("invalid document result response");
  const summaryKeys = ["page_count", "observation_count", "field_count", "table_count", "citation_count"];
  if (Object.keys(summary).length !== summaryKeys.length || !summaryKeys.every((key) => isCount((summary as Record<string, unknown>)[key]))) {
    throw new Error("invalid document result response");
  }
  const confidence = response.confidence;
  const confidenceKeys = ["input_quality", "ocr", "classification", "extraction", "validation", "overall"];
  if (confidence !== null && (!confidence || typeof confidence !== "object" || Array.isArray(confidence)
    || Object.keys(confidence).length !== confidenceKeys.length || !confidenceKeys.every((key) => isScore((confidence as Record<string, unknown>)[key])))) {
    throw new Error("invalid document result response");
  }
  if (!Array.isArray(response.warnings) || response.warnings.length > 100 || !response.warnings.every((item) => typeof item === "string")
    || !Array.isArray(response.validation_failures) || response.validation_failures.length > 100
    || !response.validation_failures.every((item) => item && typeof item === "object" && !Array.isArray(item)
      && Object.keys(item).length === 2 && typeof (item as Record<string, unknown>).code === "string"
      && ["warning", "error"].includes((item as Record<string, unknown>).severity as string))
    || !Array.isArray(response.fields) || response.fields.length > 100
    || !response.fields.every((item) => item && typeof item === "object" && !Array.isArray(item)
      && Object.keys(item).length === 4 && typeof (item as Record<string, unknown>).name === "string"
      && isScore((item as Record<string, unknown>).confidence) && isJsonValue((item as Record<string, unknown>).value)
      && isPageNumbers((item as Record<string, unknown>).pages))) {
    throw new Error("invalid document result response");
  }
  for (const key of ["provider", "model_version", "processing_profile_version"] as const) {
    if (response[key] !== null && typeof response[key] !== "string") throw new Error("invalid document result response");
  }
  if (response.duration_ms !== null && !isCount(response.duration_ms)) throw new Error("invalid document result response");
  if (response.cost !== null && (!response.cost || typeof response.cost !== "object" || Array.isArray(response.cost)
    || Object.keys(response.cost).length !== 2 || typeof (response.cost as Record<string, unknown>).currency !== "string"
    || typeof (response.cost as Record<string, unknown>).decimal !== "string")) throw new Error("invalid document result response");
  return response as DocumentJobResult;
}

export const SANDBOX_FORBIDDEN_MESSAGE = "The document sandbox is restricted to platform admins.";

export class DocumentResultNotReadyError extends Error {
  constructor() {
    super("document result is not ready");
    this.name = "DocumentResultNotReadyError";
  }
}

function rejectUnlessOk(response: Response, message: string): void {
  if (response.status === 403) throw new Error(SANDBOX_FORBIDDEN_MESSAGE);
  if (!response.ok) throw new Error(message);
}

async function readUploadState(response: Response, expectedUploadId?: string): Promise<DocumentUploadState> {
  if (response.status === 403) throw new Error(SANDBOX_FORBIDDEN_MESSAGE);
  if (!response.ok) throw new DocumentUploadError(response.status);

  try {
    return parseDocumentUploadState(await response.json(), expectedUploadId);
  } catch {
    throw new Error("invalid document upload response");
  }
}

async function readJobState(response: Response, expectedJobId?: string): Promise<DocumentJobState> {
  rejectUnlessOk(response, "document job was rejected");

  try {
    return parseDocumentJobState(await response.json(), expectedJobId);
  } catch {
    throw new Error("invalid document job response");
  }
}

async function readJobResult(response: Response, expectedJobId?: string): Promise<DocumentJobResult> {
  if (response.status === 409) throw new DocumentResultNotReadyError();
  rejectUnlessOk(response, "document result was not available");
  try {
    return parseDocumentJobResult(await response.json(), expectedJobId);
  } catch {
    throw new Error("invalid document result response");
  }
}

export async function uploadSandboxDocument(file: File): Promise<DocumentUploadState> {
  const body = new FormData();
  body.append("file", file, file.name);
  const response = await fetch("/api/document-intelligence/documents", {
    method: "POST",
    credentials: "include",
    headers: { "Idempotency-Key": crypto.randomUUID() },
    body,
  });
  return readUploadState(response);
}

export async function getSandboxDocumentStatus(uploadId: string): Promise<DocumentUploadState> {
  const response = await fetch(`/api/document-intelligence/documents/${encodeURIComponent(uploadId)}`, {
    credentials: "include",
  });
  return readUploadState(response, uploadId);
}

export async function createSandboxDocumentJob(uploadId: string): Promise<DocumentJobState> {
  const response = await fetch(`/api/document-intelligence/documents/${encodeURIComponent(uploadId)}/jobs`, {
    method: "POST",
    credentials: "include",
    headers: { "Idempotency-Key": crypto.randomUUID() },
  });
  return readJobState(response);
}

export async function getSandboxDocumentJobStatus(jobId: string): Promise<DocumentJobState> {
  const response = await fetch(`/api/document-intelligence/jobs/${encodeURIComponent(jobId)}`, {
    credentials: "include",
  });
  return readJobState(response, jobId);
}

export async function cancelSandboxDocumentJob(jobId: string): Promise<DocumentJobState> {
  const response = await fetch(`/api/document-intelligence/jobs/${encodeURIComponent(jobId)}/cancel`, {
    method: "POST",
    credentials: "include",
  });
  return readJobState(response, jobId);
}

export async function getSandboxDocumentJobResult(jobId: string): Promise<DocumentJobResult> {
  const response = await fetch(`/api/document-intelligence/jobs/${encodeURIComponent(jobId)}/result`, {
    credentials: "include",
  });
  return readJobResult(response, jobId);
}
