"use client";

import { useEffect, useRef, useState } from "react";
import { FileSearch, RefreshCw, Upload, X } from "lucide-react";

import {
  canCancelDocumentJob,
  cancelSandboxDocumentJob,
  createSandboxDocumentJob,
  documentJobProgress,
  DocumentResultNotReadyError,
  getSandboxDocumentJobResult,
  getSandboxDocumentJobStatus,
  getSandboxDocumentStatus,
  isDocumentJobActive,
  DocumentUploadError,
  parseDocumentSandboxSession,
  SANDBOX_FORBIDDEN_MESSAGE,
  serializeDocumentSandboxSession,
  type DocumentJobResult,
  type DocumentJobState,
  uploadSandboxDocument,
  type DocumentUploadState,
} from "@/lib/document-intelligence";

const TERMINAL_UPLOAD_STATUSES = new Set(["accepted", "rejected", "expired"]);
const TERMINAL_JOB_STATUSES = new Set(["cancelled", "rejected", "partial", "review_required", "completed"]);
const RESULT_READY_JOB_STATUSES = new Set(["partial", "review_required", "completed"]);
const UPLOAD_AUTO_REFRESH_LIMIT = 10;
const JOB_AUTO_REFRESH_LIMIT = 150;
const OCR_PROGRESS_STAGES = ["Queued", "Preparing", "Extracting", "Validating"];
const JOB_POLL_INTERVAL_MS = 2_500;
const MAXIMUM_ACTIVITY_EVENTS = 20;
const SANDBOX_SESSION_STORAGE_KEY = "devai.document-intelligence.sandbox-session.v1";

type ActivityEvent = {
  id: number;
  message: string;
  occurredAt: string;
};

function percentage(value: number) {
  return `${Math.round(value * 100)}%`;
}

function describe(error: unknown, fallback: string): string {
  return error instanceof Error && error.message === SANDBOX_FORBIDDEN_MESSAGE ? error.message : fallback;
}

export function DocumentSandbox() {
  const [file, setFile] = useState<File | null>(null);
  const [upload, setUpload] = useState<DocumentUploadState | null>(null);
  const [job, setJob] = useState<DocumentJobState | null>(null);
  const [result, setResult] = useState<DocumentJobResult | null>(null);
  const [activityEvents, setActivityEvents] = useState<ActivityEvent[]>([]);
  const [busy, setBusy] = useState(false);
  const [restoring, setRestoring] = useState(true);
  const [refreshes, setRefreshes] = useState(0);
  const [jobRefreshes, setJobRefreshes] = useState(0);
  const [message, setMessage] = useState("Choose a document to begin a disposable sandbox upload.");
  const refreshesRef = useRef(0);
  const jobRefreshesRef = useRef(0);
  const activityEventId = useRef(0);

  function recordActivity(message: string) {
    activityEventId.current += 1;
    setActivityEvents((events) => [
      ...events,
      { id: activityEventId.current, message, occurredAt: new Date().toISOString() },
    ].slice(-MAXIMUM_ACTIVITY_EVENTS));
  }

  function persistSession(session: { upload_id: string; job_id?: string }) {
    try {
      window.sessionStorage.setItem(SANDBOX_SESSION_STORAGE_KEY, serializeDocumentSandboxSession(session));
    } catch {
      recordActivity("This browser cannot retain the sandbox session after a refresh.");
    }
  }

  useEffect(() => {
    const session = parseDocumentSandboxSession(window.sessionStorage.getItem(SANDBOX_SESSION_STORAGE_KEY));
    if (!session) {
      setRestoring(false);
      return;
    }

    let cancelled = false;
    setMessage("Restoring the last sandbox OCR session…");
    void (async () => {
      try {
        const [restoredUpload, restoredJob] = await Promise.all([
          session.upload_id ? getSandboxDocumentStatus(session.upload_id) : Promise.resolve(null),
          session.job_id ? getSandboxDocumentJobStatus(session.job_id) : Promise.resolve(null),
        ]);
        if (cancelled) return;
        if (restoredUpload) setUpload(restoredUpload);
        if (restoredJob) {
          setJob(restoredJob);
          recordActivity(`OCR job status restored: ${restoredJob.status}.`);
          if (RESULT_READY_JOB_STATUSES.has(restoredJob.status)) {
            try {
              setResult(await getSandboxDocumentJobResult(restoredJob.job_id));
            } catch (error) {
              if (!(error instanceof DocumentResultNotReadyError)) throw error;
            }
          }
        }
        if (!cancelled) setMessage("The last sandbox OCR session was restored.");
      } catch (error) {
        if (!cancelled) {
          setMessage(describe(error, "The previous sandbox OCR session could not be restored. Try refreshing again shortly."));
        }
      } finally {
        if (!cancelled) setRestoring(false);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, []);

  async function refreshStatus(automatic = false) {
    if (!upload || busy || restoring) return;
    if (automatic && refreshesRef.current >= UPLOAD_AUTO_REFRESH_LIMIT) return;

    setBusy(true);
    try {
      const next = await getSandboxDocumentStatus(upload.upload_id);
      setUpload(next);
      setMessage(`Sandbox inspection status: ${next.status}.`);
    } catch (error) {
      setMessage(describe(error, "Sandbox inspection status is temporarily unavailable. Try refreshing again shortly."));
      recordActivity("Inspection status refresh could not be completed.");
    } finally {
      if (automatic) {
        refreshesRef.current += 1;
        setRefreshes(refreshesRef.current);
      }
      setBusy(false);
    }
  }

  async function startJob() {
    if (!upload || upload.status !== "accepted" || busy || restoring) return;

    setBusy(true);
    setMessage("Creating an OCR job through the protected DevAI sandbox…");
    try {
      const next = await createSandboxDocumentJob(upload.upload_id);
      setJob(next);
      persistSession({ upload_id: upload.upload_id, job_id: next.job_id });
      setResult(null);
      jobRefreshesRef.current = 0;
      setJobRefreshes(0);
      setMessage(`Sandbox OCR job status: ${next.status}.`);
      recordActivity(`OCR job status reported: ${next.status}.`);
    } catch (error) {
      setMessage(describe(error, "The OCR job was rejected or is temporarily unavailable."));
    } finally {
      setBusy(false);
    }
  }

  async function refreshJob(automatic = false) {
    if (!job || busy || restoring) return;
    if (automatic && jobRefreshesRef.current >= JOB_AUTO_REFRESH_LIMIT) return;

    setBusy(true);
    try {
      const next = await getSandboxDocumentJobStatus(job.job_id);
      setJob(next);
      if (next.status !== job.status) recordActivity(`OCR job status reported: ${next.status}.`);
      if (RESULT_READY_JOB_STATUSES.has(next.status) && !result) {
        try {
          setResult(await getSandboxDocumentJobResult(next.job_id));
          setMessage(`Sandbox OCR job completed with ${next.status} evidence.`);
        } catch (error) {
          setMessage(
            error instanceof DocumentResultNotReadyError
              ? `Sandbox OCR job status: ${next.status}. Result details are not ready yet.`
              : describe(error, `Sandbox OCR job status: ${next.status}. Result details could not be loaded.`),
          );
        }
      } else if (!RESULT_READY_JOB_STATUSES.has(next.status)) {
        setMessage(`Sandbox OCR job status: ${next.status}.`);
      }
    } catch (error) {
      setMessage(describe(error, "Sandbox OCR job status is temporarily unavailable. Try refreshing again shortly."));
      recordActivity("OCR status refresh could not be completed.");
    } finally {
      if (automatic) {
        jobRefreshesRef.current += 1;
        setJobRefreshes(jobRefreshesRef.current);
      }
      setBusy(false);
    }
  }

  async function cancelJob() {
    if (!job || !canCancelDocumentJob(job.status) || busy || restoring) return;

    setBusy(true);
    setMessage("Requesting durable OCR cancellation…");
    try {
      const next = await cancelSandboxDocumentJob(job.job_id);
      setJob(next);
      setMessage(`Sandbox OCR job status: ${next.status}.`);
      recordActivity(`OCR cancellation status reported: ${next.status}.`);
    } catch {
      setMessage("The OCR cancellation request is temporarily unavailable. Try again shortly.");
      recordActivity("OCR cancellation request could not be completed.");
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    if (!upload || TERMINAL_UPLOAD_STATUSES.has(upload.status) || refreshes >= UPLOAD_AUTO_REFRESH_LIMIT) return;
    const timer = window.setTimeout(() => {
      void refreshStatus(true);
    }, 2_000);
    return () => window.clearTimeout(timer);
  }, [busy, refreshes, restoring, upload]);

  useEffect(() => {
    if (!job || jobRefreshes >= JOB_AUTO_REFRESH_LIMIT) return;
    // Keep polling a result-bearing terminal status until the result itself has arrived.
    if (TERMINAL_JOB_STATUSES.has(job.status) && (result || !RESULT_READY_JOB_STATUSES.has(job.status))) return;
    const timer = window.setTimeout(() => {
      void refreshJob(true);
    }, JOB_POLL_INTERVAL_MS);
    return () => window.clearTimeout(timer);
  }, [busy, job, jobRefreshes, restoring, result]);

  async function submit() {
    if (!file || busy || restoring) return;

    setBusy(true);
    setUpload(null);
    setJob(null);
    setResult(null);
    setActivityEvents([]);
    activityEventId.current = 0;
    refreshesRef.current = 0;
    setRefreshes(0);
    jobRefreshesRef.current = 0;
    setJobRefreshes(0);
    setMessage("Uploading through the protected DevAI sandbox…");
    try {
      const next = await uploadSandboxDocument(file);
      setUpload(next);
      persistSession({ upload_id: next.upload_id });
      setMessage(`Sandbox upload status: ${next.status}.`);
      recordActivity(`Inspection status reported: ${next.status}.`);
    } catch (error) {
      setMessage(
        error instanceof DocumentUploadError
          ? error.message
          : describe(error, "The upload service is temporarily unavailable. Try again shortly."),
      );
    } finally {
      setBusy(false);
    }
  }

  const jobInProgress = job !== null && isDocumentJobActive(job.status);
  const canRefresh = upload !== null && !busy && !restoring && !jobInProgress;
  const canStartJob = upload?.status === "accepted" && !jobInProgress && !busy && !restoring;
  const canRefreshJob = jobInProgress && !busy && !restoring;
  const canCancelJob = job !== null && canCancelDocumentJob(job.status) && !busy && !restoring;
  const terminal = upload !== null && TERMINAL_UPLOAD_STATUSES.has(upload.status);
  const progress = job ? documentJobProgress(job.status) : null;
  const activeProgressIndex = progress
    ? ["queued", "preparing", "extracting", "validating"].indexOf(progress.stage)
    : -1;
  const successfulTerminal = job?.status === "completed"
    || job?.status === "partial"
    || job?.status === "review_required";

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
            disabled={busy || restoring || jobInProgress}
            onChange={(event) => {
              const selectedFile = event.target.files?.item(0) ?? null;
              setFile(selectedFile);
              setMessage(selectedFile
                ? "Document selected. Next, click \"Upload selected document\" to send it for sandbox inspection."
                : "Choose a document to begin a disposable sandbox upload.");
            }}
            className="mt-2 block w-full text-sm"
          />
          <p className="mt-2 text-xs" style={{ color: "var(--ink-muted)" }}>
            File type and content are verified by the service; the browser selection is not a security decision.
          </p>
          <p className="mt-2 text-xs font-medium" style={{ color: "var(--ink-soft)" }}>
            Step 1: choose a file. Step 2: click the upload button below to send it for inspection.
          </p>
        </div>

        <div className="mt-5 flex flex-wrap gap-3">
          <button type="button" className="btn-primary" disabled={!file || busy || restoring || jobInProgress} onClick={submit}>
            <Upload className="h-4 w-4" /> {busy && !upload ? "Uploading selected document…" : "2. Upload selected document"}
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
          <button type="button" className="btn-secondary" disabled={!canCancelJob} onClick={() => void cancelJob()}>
            <X className="h-4 w-4" /> Cancel OCR
          </button>
        </div>

        {jobInProgress && (
          <p className="mt-3 text-xs" style={{ color: "var(--ink-muted)" }}>
            OCR is active. Document selection, upload, inspection refresh, and starting another OCR job are locked until this job finishes or is cancelled.
          </p>
        )}

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
            <>
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
              {progress && (
                <section className="mt-4" aria-label="Live OCR progress" aria-live="polite">
                  <p className="label-eyebrow">Live OCR progress</p>
                  <p className="mt-1 text-xs" style={{ color: "var(--ink-soft)" }}>{progress.message} Refreshing automatically for up to five minutes.</p>
                  <ol className="mt-3 grid gap-2 sm:grid-cols-4">
                    {OCR_PROGRESS_STAGES.map((stage, index) => {
                      const complete = successfulTerminal || index < activeProgressIndex;
                      const active = index === activeProgressIndex;
                      return (
                        <li key={stage} className="rounded border px-3 py-2 text-xs" style={{ borderColor: active ? "var(--accent)" : "var(--border-subtle)" }}>
                          <p className="label-eyebrow">{complete ? "Complete" : active ? "Current" : "Waiting"}</p>
                          <p className="mt-1 font-medium" style={{ color: "var(--ink-strong)" }}>{stage}</p>
                        </li>
                      );
                    })}
                  </ol>
                </section>
              )}
              {activityEvents.length > 0 && (
                <section className="mt-4" aria-label="OCR activity events" aria-live="polite">
                  <p className="label-eyebrow">OCR activity events</p>
                  <p className="mt-1 text-xs" style={{ color: "var(--ink-muted)" }}>
                    Safe lifecycle events observed by this browser session. Worker logs and document contents are not exposed here.
                  </p>
                  <ol className="mt-3 space-y-2 border-l pl-4 text-xs" style={{ borderColor: "var(--border-subtle)" }}>
                    {activityEvents.map((event) => (
                      <li key={event.id}>
                        <p style={{ color: "var(--ink-strong)" }}>{event.message}</p>
                        <p className="mt-1 font-mono" style={{ color: "var(--ink-muted)" }}>{new Date(event.occurredAt).toLocaleTimeString()}</p>
                      </li>
                    ))}
                  </ol>
                </section>
              )}
            </>
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
