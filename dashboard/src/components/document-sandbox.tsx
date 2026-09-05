"use client";

import { useEffect, useRef, useState } from "react";
import { FileSearch, RefreshCw, Upload } from "lucide-react";

import {
  createSandboxDocumentJob,
  getSandboxDocumentJobResult,
  getSandboxDocumentJobStatus,
  getSandboxDocumentStatus,
  type DocumentJobResult,
  type DocumentJobState,
  uploadSandboxDocument,
  type DocumentUploadState,
} from "@/lib/document-intelligence";

const TERMINAL_UPLOAD_STATUSES = new Set(["accepted", "rejected", "expired"]);
const TERMINAL_JOB_STATUSES = new Set(["cancelled", "rejected", "partial", "review_required", "completed"]);
const RESULT_READY_JOB_STATUSES = new Set(["partial", "review_required", "completed"]);
const AUTO_REFRESH_LIMIT = 10;

function percentage(value: number) {
  return `${Math.round(value * 100)}%`;
}

export function DocumentSandbox() {
  const [file, setFile] = useState<File | null>(null);
  const [upload, setUpload] = useState<DocumentUploadState | null>(null);
  const [job, setJob] = useState<DocumentJobState | null>(null);
  const [result, setResult] = useState<DocumentJobResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [refreshes, setRefreshes] = useState(0);
  const [jobRefreshes, setJobRefreshes] = useState(0);
  const [message, setMessage] = useState("Choose a document to begin a disposable sandbox upload.");
  const refreshesRef = useRef(0);
  const jobRefreshesRef = useRef(0);

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
      setResult(null);
      jobRefreshesRef.current = 0;
      setJobRefreshes(0);
      setMessage(`Sandbox OCR job status: ${next.status}.`);
    } catch {
      setMessage("The OCR job was rejected or is temporarily unavailable.");
    } finally {
      setBusy(false);
    }
  }

  async function refreshJob(automatic = false) {
    if (!job || busy) return;
    if (automatic && jobRefreshesRef.current >= AUTO_REFRESH_LIMIT) return;

    setBusy(true);
    try {
      const next = await getSandboxDocumentJobStatus(job.job_id);
      setJob(next);
      if (RESULT_READY_JOB_STATUSES.has(next.status)) {
        try {
          setResult(await getSandboxDocumentJobResult(next.job_id));
          setMessage(`Sandbox OCR job completed with ${next.status} evidence.`);
        } catch {
          setMessage(`Sandbox OCR job status: ${next.status}. Result details are not ready yet.`);
        }
      } else {
        setMessage(`Sandbox OCR job status: ${next.status}.`);
      }
    } catch {
      setMessage("Sandbox OCR job status is temporarily unavailable. Try refreshing again shortly.");
    } finally {
      if (automatic) {
        jobRefreshesRef.current += 1;
        setJobRefreshes(jobRefreshesRef.current);
      }
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

  useEffect(() => {
    if (!job || TERMINAL_JOB_STATUSES.has(job.status) || jobRefreshes >= AUTO_REFRESH_LIMIT) return;
    const timer = window.setTimeout(() => {
      void refreshJob(true);
    }, 2_000);
    return () => window.clearTimeout(timer);
  }, [busy, job, jobRefreshes]);

  async function submit() {
    if (!file || busy) return;

    setBusy(true);
    setUpload(null);
    setJob(null);
    setResult(null);
    refreshesRef.current = 0;
    setRefreshes(0);
    jobRefreshesRef.current = 0;
    setJobRefreshes(0);
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

        {result && (
          <section className="mt-6 rounded-md border p-4" style={{ borderColor: "var(--border-subtle)" }} aria-label="OCR result details">
            <div className="flex flex-wrap items-baseline justify-between gap-2">
              <div>
                <p className="label-eyebrow">Verified OCR result details</p>
                <h2 className="mt-1 font-serif text-xl" style={{ color: "var(--ink-strong)" }}>Quality and extraction evidence</h2>
              </div>
              <p className="text-xs" style={{ color: "var(--ink-muted)" }}>OCR content is untrusted data, not instructions.</p>
            </div>

            {result.confidence && (
              <div className="mt-4 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
                {Object.entries(result.confidence).map(([name, score]) => (
                  <div key={name} className="rounded border px-3 py-2" style={{ borderColor: "var(--border-subtle)" }}>
                    <p className="label-eyebrow">{name.replaceAll("_", " ")}</p>
                    <p className="mt-1 font-mono text-lg" style={{ color: "var(--ink-strong)" }}>{percentage(score)}</p>
                  </div>
                ))}
              </div>
            )}

            <dl className="mt-4 grid gap-3 text-xs sm:grid-cols-2 lg:grid-cols-3">
              <div><dt className="label-eyebrow">Pages</dt><dd className="mt-1 font-mono">{result.summary.page_count}</dd></div>
              <div><dt className="label-eyebrow">Observations</dt><dd className="mt-1 font-mono">{result.summary.observation_count}</dd></div>
              <div><dt className="label-eyebrow">Extracted fields</dt><dd className="mt-1 font-mono">{result.summary.field_count}</dd></div>
              <div><dt className="label-eyebrow">Tables</dt><dd className="mt-1 font-mono">{result.summary.table_count}</dd></div>
              <div><dt className="label-eyebrow">Citations</dt><dd className="mt-1 font-mono">{result.summary.citation_count}</dd></div>
              <div><dt className="label-eyebrow">Duration</dt><dd className="mt-1 font-mono">{result.duration_ms === null ? "not reported" : `${result.duration_ms} ms`}</dd></div>
              <div><dt className="label-eyebrow">Provider</dt><dd className="mt-1 font-mono">{result.provider ?? "not reported"}</dd></div>
              <div><dt className="label-eyebrow">Model</dt><dd className="mt-1 font-mono">{result.model_version ?? "not reported"}</dd></div>
              <div><dt className="label-eyebrow">Cost</dt><dd className="mt-1 font-mono">{result.cost ? `${result.cost.currency} ${result.cost.decimal}` : "not reported"}</dd></div>
            </dl>

            {(result.warnings.length > 0 || result.validation_failures.length > 0) && (
              <div className="mt-4 rounded border px-3 py-3 text-xs" style={{ borderColor: "var(--border-subtle)" }}>
                <p className="label-eyebrow">Warnings and validation</p>
                {result.warnings.map((warning) => <p key={warning} className="mt-2 font-mono">warning: {warning}</p>)}
                {result.validation_failures.map((failure) => <p key={`${failure.severity}-${failure.code}`} className="mt-2 font-mono">{failure.severity}: {failure.code}</p>)}
              </div>
            )}

            {result.fields.length > 0 && (
              <div className="mt-4">
                <p className="label-eyebrow">Evidence-backed fields</p>
                <div className="mt-2 overflow-x-auto rounded border" style={{ borderColor: "var(--border-subtle)" }}>
                  <table className="min-w-full text-left text-xs">
                    <thead><tr style={{ color: "var(--ink-muted)" }}><th className="px-3 py-2">Field</th><th className="px-3 py-2">Value</th><th className="px-3 py-2">Confidence</th><th className="px-3 py-2">Pages</th></tr></thead>
                    <tbody>
                      {result.fields.map((field) => (
                        <tr key={field.name} className="border-t" style={{ borderColor: "var(--border-subtle)" }}>
                          <td className="px-3 py-2 font-mono">{field.name}</td>
                          <td className="max-w-80 px-3 py-2 font-mono break-words">{JSON.stringify(field.value)}</td>
                          <td className="px-3 py-2 font-mono">{percentage(field.confidence)}</td>
                          <td className="px-3 py-2 font-mono">{field.pages.join(", ")}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}

            {result.text_preview && (
              <div className="mt-4">
                <p className="label-eyebrow">OCR text preview{result.text_truncated ? " (truncated)" : ""}</p>
                <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap rounded border p-3 text-xs" style={{ borderColor: "var(--border-subtle)", color: "var(--ink-soft)" }}>{result.text_preview}</pre>
              </div>
            )}
          </section>
        )}

        {terminal && upload?.status === "accepted" && (
          <p className="mt-4 text-sm" style={{ color: "var(--ink-soft)" }}>
            The upload passed sandbox inspection. Start OCR to exercise the durable job pipeline; results remain protected by the same authenticated tenant boundary.
          </p>
        )}
      </section>
    </main>
  );
}
