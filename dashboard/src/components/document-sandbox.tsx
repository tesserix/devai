"use client";

import { useEffect, useRef, useState } from "react";
import { FileSearch, RefreshCw, Upload } from "lucide-react";

import {
  createSandboxDocumentJob,
  getSandboxDocumentJobStatus,
  getSandboxDocumentStatus,
  type DocumentJobState,
  uploadSandboxDocument,
  type DocumentUploadState,
} from "@/lib/document-intelligence";

const TERMINAL_UPLOAD_STATUSES = new Set(["accepted", "rejected", "expired"]);
const AUTO_REFRESH_LIMIT = 10;

export function DocumentSandbox() {
  const [file, setFile] = useState<File | null>(null);
  const [upload, setUpload] = useState<DocumentUploadState | null>(null);
  const [job, setJob] = useState<DocumentJobState | null>(null);
  const [busy, setBusy] = useState(false);
  const [refreshes, setRefreshes] = useState(0);
  const [message, setMessage] = useState("Choose a document to begin a disposable sandbox upload.");
  const refreshesRef = useRef(0);

  async function refreshStatus(automatic = false) {
    if (!upload || busy) return;
    if (automatic && refreshesRef.current >= AUTO_REFRESH_LIMIT) return;

    setBusy(true);
    try {
      const next = await getSandboxDocumentStatus(upload.upload_id);
      setUpload(next);
      setMessage(`Sandbox inspection status: ${next.status}.`);
    } catch {
      setMessage("Sandbox inspection status is temporarily unavailable. Try refreshing again shortly.");
    } finally {
      if (automatic) {
        refreshesRef.current += 1;
        setRefreshes(refreshesRef.current);
      }
      setBusy(false);
    }
  }

  async function startJob() {
    if (!upload || upload.status !== "accepted" || busy) return;

    setBusy(true);
    setMessage("Creating an OCR job through the protected DevAI sandbox…");
    try {
      const next = await createSandboxDocumentJob(upload.upload_id);
      setJob(next);
      setMessage(`Sandbox OCR job status: ${next.status}.`);
    } catch {
      setMessage("The OCR job was rejected or is temporarily unavailable.");
    } finally {
      setBusy(false);
    }
  }

  async function refreshJob() {
    if (!job || busy) return;

    setBusy(true);
    try {
      const next = await getSandboxDocumentJobStatus(job.job_id);
      setJob(next);
      setMessage(`Sandbox OCR job status: ${next.status}.`);
    } catch {
      setMessage("Sandbox OCR job status is temporarily unavailable. Try refreshing again shortly.");
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    if (!upload || TERMINAL_UPLOAD_STATUSES.has(upload.status) || refreshes >= AUTO_REFRESH_LIMIT) return;
    const timer = window.setTimeout(() => {
      void refreshStatus(true);
    }, 2_000);
    return () => window.clearTimeout(timer);
  }, [busy, refreshes, upload]);

  async function submit() {
    if (!file || busy) return;

    setBusy(true);
    setUpload(null);
    setJob(null);
    refreshesRef.current = 0;
    setRefreshes(0);
    setMessage("Uploading through the protected DevAI sandbox…");
    try {
      const next = await uploadSandboxDocument(file);
      setUpload(next);
      setMessage(`Sandbox upload status: ${next.status}.`);
    } catch {
      setMessage("Document upload was rejected or is temporarily unavailable.");
    } finally {
      setBusy(false);
    }
  }

  const canRefresh = upload !== null && !busy;
  const canStartJob = upload?.status === "accepted" && !job && !busy;
  const canRefreshJob = job !== null && !busy;
  const terminal = upload !== null && TERMINAL_UPLOAD_STATUSES.has(upload.status);

  return (
    <main className="mx-auto w-full max-w-3xl px-5 py-8">
      <section className="panel p-6">
        <div className="flex items-start gap-3">
          <FileSearch className="mt-0.5 h-5 w-5 shrink-0" style={{ color: "var(--accent)" }} />
          <div>
            <p className="label-eyebrow">DevAI test surface</p>
            <h1 className="mt-1 font-serif text-2xl" style={{ color: "var(--ink-strong)" }}>
              Document intelligence sandbox
            </h1>
            <p className="mt-2 text-sm" style={{ color: "var(--ink-soft)" }}>
              Upload a PDF, PNG, JPEG, TIFF, or WebP to the isolated test bucket. Sandbox documents are deleted after 24 hours and never become production training data.
            </p>
          </div>
        </div>

        <div className="mt-6">
          <label htmlFor="document" className="label-eyebrow block">
            Document
          </label>
          <input
            id="document"
            type="file"
            accept="application/pdf,image/png,image/jpeg,image/tiff,image/webp"
            onChange={(event) => setFile(event.target.files?.item(0) ?? null)}
            className="mt-2 block w-full text-sm"
          />
          <p className="mt-2 text-xs" style={{ color: "var(--ink-muted)" }}>
            File type and content are verified by the service; the browser selection is not a security decision.
          </p>
        </div>

        <div className="mt-5 flex flex-wrap gap-3">
          <button type="button" className="btn-primary" disabled={!file || busy} onClick={submit}>
            <Upload className="h-4 w-4" /> {busy && !upload ? "Uploading…" : "Upload for inspection"}
          </button>
          <button type="button" className="btn-secondary" disabled={!canRefresh} onClick={() => void refreshStatus()}>
            <RefreshCw className="h-4 w-4" /> Refresh status
          </button>
          <button type="button" className="btn-secondary" disabled={!canStartJob} onClick={() => void startJob()}>
            <FileSearch className="h-4 w-4" /> Start OCR
          </button>
          <button type="button" className="btn-secondary" disabled={!canRefreshJob} onClick={() => void refreshJob()}>
            <RefreshCw className="h-4 w-4" /> Refresh OCR
          </button>
        </div>

        <div className="mt-6 rounded-md border px-4 py-3 text-sm" style={{ borderColor: "var(--border-subtle)" }} aria-live="polite">
          <p style={{ color: "var(--ink-soft)" }}>{message}</p>
          {upload && (
            <dl className="mt-3 grid gap-2 text-xs sm:grid-cols-2">
              <div>
                <dt className="label-eyebrow">Upload ID</dt>
                <dd className="mt-1 font-mono" style={{ color: "var(--ink-strong)" }}>{upload.upload_id}</dd>
              </div>
              <div>
                <dt className="label-eyebrow">Lifecycle</dt>
                <dd className="mt-1 font-mono" style={{ color: "var(--ink-strong)" }}>{upload.status}</dd>
              </div>
            </dl>
          )}
          {job && (
            <dl className="mt-3 grid gap-2 text-xs sm:grid-cols-2">
              <div>
                <dt className="label-eyebrow">OCR job ID</dt>
                <dd className="mt-1 font-mono" style={{ color: "var(--ink-strong)" }}>{job.job_id}</dd>
              </div>
              <div>
                <dt className="label-eyebrow">OCR lifecycle</dt>
                <dd className="mt-1 font-mono" style={{ color: "var(--ink-strong)" }}>{job.status}</dd>
              </div>
            </dl>
          )}
        </div>

        {terminal && upload?.status === "accepted" && (
          <p className="mt-4 text-sm" style={{ color: "var(--ink-soft)" }}>
            The upload passed sandbox inspection. Start OCR to exercise the durable job pipeline; results remain protected by the same authenticated tenant boundary.
          </p>
        )}
      </section>
    </main>
  );
}
