"use client";

/**
 * Per-run detail page — the dashboard analog of Fiber's Mission Control
 * task view. Lays out four tabs along the top of a right-hand pane:
 *
 *   TIMELINE   — A2A messages + stage transitions (existing a2a-feed).
 *   CHAT       — Conversational interface scoped to this run (existing chat-panel).
 *   PREVIEW    — Live iframe of the running app at preview_url.
 *   REPO       — Read-only file tree + viewer that updates in real time
 *                as agents commit files (SSE on /api/runs/{id}/repo/events).
 *
 * The page is intentionally simple: each tab pane is a self-contained
 * component so the four can evolve independently. The Repo tab is the
 * new piece — see RepoPanel below for the file tree + viewer + SSE wiring.
 */

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { api, type PipelineRun } from "@/lib/api";
import { A2AFeed } from "@/components/a2a-feed";
import { ChatPanel } from "@/components/chat-panel";
import { RepoPanel } from "@/components/repo-panel";

type Tab = "timeline" | "chat" | "preview" | "repo";

interface A2AMessage {
  id: string;
  from_agent: string;
  to_agent: string;
  message_type: string;
  subject: string;
  body: string;
  timestamp: string;
}

interface RunDetail extends PipelineRun {
  triggered_by?: string;
  trace_id?: string;
  context?: { preview_url?: string; editor_url?: string };
}

export default function RunDetailPage() {
  const params = useParams<{ id: string }>();
  const runId = String(params?.id ?? "");
  const [tab, setTab] = useState<Tab>("timeline");
  const [run, setRun] = useState<RunDetail | null>(null);
  const [a2aMessages, setA2aMessages] = useState<A2AMessage[]>([]);

  useEffect(() => {
    if (!runId) return;
    let cancelled = false;

    const refresh = async () => {
      try {
        const data = (await api.getRun(runId)) as RunDetail;
        if (!cancelled) setRun(data);
      } catch {
        // The run may not be queryable through the blueprint API if
        // it's a legacy LangGraph run — leave run=null and let the tabs
        // degrade gracefully (Repo tab still works via run-id resolution).
      }
      try {
        const res = await fetch(`/dashboard/api/pipeline/runs/${encodeURIComponent(runId)}/a2a`, {
          credentials: "include",
        });
        if (!cancelled && res.ok) {
          const list: A2AMessage[] = await res.json();
          setA2aMessages(list);
        }
      } catch {
        // A2A endpoint may not exist on every backend — non-fatal.
      }
    };

    refresh();
    const id = window.setInterval(refresh, 5000);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [runId]);

  const previewUrl = run?.context?.preview_url || "";
  const editorUrl = run?.context?.editor_url || "";

  return (
    <div
      className="flex flex-col h-full min-h-0"
      style={{ background: "var(--surface)", color: "var(--ink)" }}
    >
      {/* Header — run identity */}
      <div
        className="px-6 py-3 flex items-center justify-between"
        style={{ borderBottom: "1px solid var(--border)" }}
      >
        <div className="flex items-center gap-3 min-w-0">
          <span
            className="text-xs font-mono px-2 py-0.5 rounded"
            style={{ background: "var(--surface-muted)", color: "var(--ink-soft)" }}
          >
            {runId}
          </span>
          <span className="text-sm font-medium truncate" style={{ color: "var(--ink-strong)" }}>
            {run?.repo || "—"}
          </span>
          {run?.triggered_by && (
            <span className="text-xs" style={{ color: "var(--ink-muted)" }}>
              triggered by <code>{run.triggered_by}</code>
            </span>
          )}
        </div>
        <span
          className="text-xs px-2 py-0.5 rounded font-mono"
          style={{
            background: "var(--accent-soft-bg)",
            color: "var(--accent)",
            border: "1px solid var(--accent-soft-bd)",
          }}
        >
          {run?.stage || "loading"}
        </span>
      </div>

      {/* Tab bar */}
      <div
        className="px-6 flex items-center gap-1 text-xs font-medium uppercase tracking-wide"
        style={{ borderBottom: "1px solid var(--border)" }}
      >
        {(["timeline", "chat", "preview", "repo"] as const).map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className="px-3 py-2.5 transition-colors"
            style={{
              color: tab === t ? "var(--ink-strong)" : "var(--ink-muted)",
              borderBottom: tab === t ? "2px solid var(--accent)" : "2px solid transparent",
            }}
          >
            {t}
          </button>
        ))}
      </div>

      {/* Tab content */}
      <div className="flex-1 min-h-0 overflow-hidden">
        {tab === "timeline" && (
          <div className="h-full overflow-y-auto p-6">
            <A2AFeed messages={a2aMessages} />
          </div>
        )}
        {tab === "chat" && (
          // Chat on the left, live preview on the right — edits made in chat
          // hot-reload the preview beside it.
          <div className="h-full flex">
            <div className="flex-1 min-w-0">
              <ChatPanel runId={runId} repo={run?.repo} stage={run?.stage} />
            </div>
            <div className="w-px shrink-0" style={{ background: "var(--border)" }} />
            <div className="flex-1 min-w-0">
              <PreviewPane url={previewUrl} editorUrl={editorUrl} repo={run?.repo} />
            </div>
          </div>
        )}
        {tab === "preview" && (
          <PreviewPane url={previewUrl} editorUrl={editorUrl} repo={run?.repo} />
        )}
        {tab === "repo" && <RepoPanel runId={runId} />}
      </div>
    </div>
  );
}

function PreviewPane({ url, editorUrl, repo }: { url: string; editorUrl: string; repo?: string }) {
  // The pipeline-run URL (from spin_preview_pod) takes precedence; otherwise
  // the user can start an on-demand preview for this repo. Both resolve to the
  // same unique forwarded host (preview-<id>.tesserix.app).
  const [liveUrl, setLiveUrl] = useState(url);
  const [starting, setStarting] = useState(false);
  const [err, setErr] = useState("");

  useEffect(() => {
    if (url) setLiveUrl(url);
  }, [url]);

  async function start() {
    if (!repo) return;
    setStarting(true);
    setErr("");
    try {
      const s = await api.preview.start(repo, "main");
      setLiveUrl(s.preview_url || s.fe_url || "");
    } catch (e) {
      setErr(String((e as Error)?.message ?? e));
    } finally {
      setStarting(false);
    }
  }

  if (!liveUrl) {
    return (
      <div
        className="h-full flex flex-col items-center justify-center gap-3 text-sm"
        style={{ color: "var(--ink-muted)" }}
      >
        <p>No live preview yet for {repo || "this run"}.</p>
        {repo && (
          <button className="btn-primary !py-1.5 text-xs" disabled={starting} onClick={start}>
            {starting ? "Starting…" : "Start preview"}
          </button>
        )}
        {err && <p style={{ color: "var(--error)" }}>{err}</p>}
      </div>
    );
  }
  return (
    <div className="h-full flex flex-col">
      <div
        className="px-4 py-2 flex items-center gap-3 text-xs font-mono"
        style={{ borderBottom: "1px solid var(--border)", background: "var(--surface-muted)" }}
      >
        <span style={{ color: "var(--ink-muted)" }}>preview:</span>
        <a
          href={liveUrl}
          target="_blank"
          rel="noreferrer"
          className="truncate"
          style={{ color: "var(--accent)" }}
        >
          {liveUrl}
        </a>
        {editorUrl && (
          <a href={editorUrl} target="_blank" rel="noreferrer" style={{ color: "var(--accent)" }}>
            editor
          </a>
        )}
        {/* Open the SAME unique forwarded URL standalone in a new tab. */}
        <a
          href={liveUrl}
          target="_blank"
          rel="noreferrer"
          className="ml-auto shrink-0 btn-secondary !py-1 !px-2"
        >
          Open in new tab ↗
        </a>
      </div>
      <iframe
        src={liveUrl}
        className="flex-1 w-full"
        style={{ border: "none", background: "white" }}
        sandbox="allow-scripts allow-same-origin allow-forms allow-popups"
        title="Live preview"
      />
    </div>
  );
}
