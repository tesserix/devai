export const DOCUMENT_UPLOAD_STATUSES = [
  "reserved",
  "uploaded",
  "inspecting",
  "accepted",
  "rejected",
  "expired",
] as const;

export type DocumentUploadStatus = (typeof DOCUMENT_UPLOAD_STATUSES)[number];

export type DocumentUploadState = {
  upload_id: string;
  status: DocumentUploadStatus;
};

function isDocumentUploadStatus(value: unknown): value is DocumentUploadStatus {
  return typeof value === "string" && DOCUMENT_UPLOAD_STATUSES.includes(value as DocumentUploadStatus);
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

async function readUploadState(response: Response, expectedUploadId?: string): Promise<DocumentUploadState> {
  if (!response.ok) {
    throw new Error("document request was rejected");
  }

  try {
    return parseDocumentUploadState(await response.json(), expectedUploadId);
  } catch {
    throw new Error("invalid document upload response");
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
