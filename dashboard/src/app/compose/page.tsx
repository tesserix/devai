"use client";

import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import { ArrowUpRight, Send, Terminal, Users, Wifi, WifiOff } from "lucide-react";
import Link from "next/link";
import {
  api,
  type BlueprintSummary,
  type Crew,
  type StreamEvent,
  type Team,
  type ContextRef,
} from "@/lib/api";
import { MentionInput, type MentionSuggestion } from "@/components/mention-input";
import { AttachmentUpload, type Attachment } from "@/components/attachment-upload";
import { TerminalPanel } from "@/components/terminal-panel";
import { CheckpointTimeline, type Checkpoint } from "@/components/checkpoint-timeline";
import { BlueprintPicker } from "@/components/blueprint-picker";
import { RepoPicker } from "@/components/repo-picker";
import { HelpPopover } from "@/components/guidance";
import { RunStateBadge, normalizeRunState, type RunState } from "@/components/run-state-badge";

/**
 * Cursor-style composer. Type an intent, @-mention files/teammates, pick a
 * blueprint + team → crew, hit Run, and watch the terminal stream + the
 * checkpoint timeline fill in live as the crew works.
 *
 * DASH-6  — the blueprint is now an explicit choice (BlueprintPicker) instead of
 *           a hardcoded "crew-task", so the backend never silently defaults.
 * DASH-10 — attachments pass FILENAMES as hints only (no upload endpoint yet);
 *           AttachmentUpload is honest about that rather than pretending the
 *           image bytes were uploaded.
 * DASH-11 — the live SSE stream reconnects with replay + Last-Event-ID so a
 *           dropped connection backfills missed frames, and the terminal/spinner
 *           are driven off an explicit run-state poll rather than guessing from
 *           a single "post-report" frame.
 */

const SSE_REPLAY = 50; // frames the server replays on (re-)connect

type Conn = "idle" | "connecting" | "open" | "closed";

export default function ComposePage() {
  return (
    <Suspense fallback={null}>
      <ComposeInner />
    </Suspense>
  );
}

function ComposeInner() {
  const searchParams = useSearchParams();
  const [intent, setIntent] = useState("");
  const [repo, setRepo] = useState("");

  // Deep-link target: ⌘K @ (and any other surface) can hand us a repo to
  // pre-select via /compose?repo=owner/name. Only seed once, on mount.
  useEffect(() => {
    const r = searchParams.get("repo");
    if (r) setRepo(r);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const [blueprints, setBlueprints] = useState<BlueprintSummary[]>([]);
  const [loadingBlueprints, setLoadingBlueprints] = useState(false);
  const [blueprint, setBlueprint] = useState<string | null>(null);

  const [teams, setTeams] = useState<Team[]>([]);
  const [teamId, setTeamId] = useState("");
  const [crews, setCrews] = useState<Crew[]>([]);
  const [crewId, setCrewId] = useState("");

  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [suggestions, setSuggestions] = useState<MentionSuggestion[]>([]);

  const [taskId, setTaskId] = useState<string | null>(null);
  const [events, setEvents] = useState<StreamEvent[]>([]);
  const [checkpoints, setCheckpoints] = useState<Checkpoint[]>([]);
  const [running, setRunning] = useState(false);
  const [runState, setRunState] = useState<RunState | null>(null);
  const [conn, setConn] = useState<Conn>("idle");
  const [error, setError] = useState<string | null>(null);

  // Last server event id seen on this stream — fed back on reconnect so the
  // server can backfill anything emitted while we were disconnected.
  const lastEventIdRef = useRef<string>("");

  // Load teams + blueprints once.
  useEffect(() => {
    api.listTeams().then(setTeams).catch(() => setTeams([]));
  }, []);

  useEffect(() => {
    setLoadingBlueprints(true);
    api
      .listBlueprints()
      .then((bs) => {
        setBlueprints(bs);
        // Default to crew-task (the composer's natural fit) if present, else the
        // first available — never a phantom name the catalog doesn't ship.
        setBlueprint((cur) =>
          cur ?? (bs.find((b) => b.name === "crew-task")?.name ?? bs[0]?.name ?? null),
        );
      })
      .catch(() => setBlueprints([]))
      .finally(() => setLoadingBlueprints(false));
  }, []);

  // When the team changes, load its crews + members (for @-mentions).
  useEffect(() => {
    if (!teamId) {
      setCrews([]);
      return;
    }
    api
      .listCrews(teamId)
      .then((c) => {
        setCrews(c);
        if (c.length && !crewId) setCrewId(c[0].id);
      })
      .catch(() => setCrews([]));
  }, [teamId]); // eslint-disable-line react-hooks/exhaustive-deps

  // The selected crew's members become @-mention suggestions.
  useEffect(() => {
    const crew = crews.find((c) => c.id === crewId);
    const members: MentionSuggestion[] = (crew?.members || []).map((m) => ({
      value: m.specialization,
      hint: m.role_label || "member",
      kind: "member",
    }));
    setSuggestions(members);
  }, [crews, crewId]);

  // ── Live event stream (DASH-11) ───────────────────────────────────────
  // Subscribe once a task is dispatched. We let the native EventSource handle
  // fast reconnects (it replays Last-Event-ID automatically), and on every
  // (re)connect we ask the server to replay recent frames + anything after the
  // last id we saw, so a dropped socket backfills instead of leaving a hole.
  useEffect(() => {
    if (!taskId) return;
    let es: EventSource | null = null;
    let closedByUs = false;

    const connect = () => {
      const params = new URLSearchParams({ replay: String(SSE_REPLAY) });
      // task scope + explicit backfill cursor (belt-and-suspenders alongside the
      // browser's own Last-Event-ID header).
      params.set("task_id", taskId);
      if (lastEventIdRef.current) params.set("last_event_id", lastEventIdRef.current);
      setConn("connecting");
      es = new EventSource(`/api/pipeline/events/stream?${params.toString()}`, {
        withCredentials: true,
      });

      es.onopen = () => setConn("open");

      es.addEventListener("stage", (e) => {
        const me = e as MessageEvent;
        if (me.lastEventId) lastEventIdRef.current = me.lastEventId;
        try {
          const data = JSON.parse(me.data) as StreamEvent;
          if (data.task_id !== taskId) return;
          setEvents((prev) => dedupeAppend(prev, data));
          if (data.checkpoint) {
            setCheckpoints((prev) => {
              const sha = data.checkpoint as string;
              if (prev.some((c) => c.sha === sha)) return prev; // replay-safe
              return [
                ...prev,
                {
                  sha,
                  label: data.message || data.stage || "checkpoint",
                  stage: data.stage,
                  ts: data.timestamp,
                },
              ];
            });
          }
        } catch {
          /* ignore malformed frames */
        }
      });

      es.onerror = () => {
        // The browser auto-reconnects; reflect the gap in the UI. We don't tear
        // the socket down here — done/failed is decided by the poll below.
        if (!closedByUs) setConn("closed");
      };
    };

    connect();
    return () => {
      closedByUs = true;
      es?.close();
      setConn("idle");
    };
  }, [taskId]);

  // ── Run-state poll (DASH-11) ──────────────────────────────────────────
  // The SSE stream tells us *what's happening*; the authoritative done/failed
  // signal is the run record. Poll it while running so a missed terminal frame
  // can't leave the spinner stuck forever.
  useEffect(() => {
    if (!taskId || !running) return;
    let cancelled = false;

    const poll = async () => {
      try {
        const run = await api.getRun(taskId);
        if (cancelled) return;
        const st = normalizeRunState(run.stage);
        setRunState(st);
        if (st === "done" || st === "failed" || st === "cancelled") {
          setRunning(false);
        }
      } catch {
        /* transient — keep polling */
      }
    };

    poll();
    const id = window.setInterval(poll, 4000);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [taskId, running]);

  const parseContextRefs = useCallback(
    (text: string): ContextRef[] => {
      const refs: ContextRef[] = [];
      const seen = new Set<string>();
      const re = /@([\w./-]+)/g;
      let m: RegExpExecArray | null;
      while ((m = re.exec(text)) !== null) {
        const token = m[1];
        if (seen.has(token)) continue;
        seen.add(token);
        const sug = suggestions.find((s) => s.value === token);
        if (sug?.kind === "member") continue; // members are attribution, not context
        const kind = token.startsWith("http") ? "url" : token.includes(".") ? "file" : "symbol";
        refs.push({ type: kind, ref: token });
      }
      return refs;
    },
    [suggestions],
  );

  async function run() {
    if (!intent.trim() || !repo.trim()) {
      setError("Intent and repo are required.");
      return;
    }
    if (!blueprint) {
      setError("Pick a blueprint to run.");
      return;
    }
    setError(null);
    setRunning(true);
    setRunState("queued");
    setEvents([]);
    setCheckpoints([]);
    lastEventIdRef.current = "";
    try {
      const result = await api.dispatchCompose({
        intent,
        repo,
        blueprint,
        team_id: teamId || undefined,
        crew_id: crewId || undefined,
        context_refs: parseContextRefs(intent),
        // DASH-10: filenames only — there is no object-store upload yet, so we
        // pass them as hints rather than pretending the bytes were uploaded.
        attachments: attachments.map((a) => a.file.name),
        label: "composer",
      });
      setTaskId(result.task_id);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setRunning(false);
      setRunState(null);
    }
  }

  function rollback(sha: string) {
    if (!taskId) return;
    api.rollbackTo(taskId, sha).catch((e) => setError(String(e)));
  }

  const selectedCrew = crews.find((c) => c.id === crewId);

  return (
    <div className="w-full p-6">
      <header className="mb-5">
        <h1
          className="flex items-center gap-1.5 text-xl font-semibold"
          style={{ color: "var(--ink-strong)" }}
        >
          Compose
          <HelpPopover term="run" />
        </h1>
        <p className="text-sm" style={{ color: "var(--ink-muted)" }}>
          Point a crew at a task — @-mention files, pick a blueprint, watch it build.
        </p>
      </header>

      <div className="grid grid-cols-1 gap-5 lg:grid-cols-[1fr_320px]">
        {/* Composer card */}
        <div
          className="rounded-xl border p-4"
          style={{ background: "var(--surface)", borderColor: "var(--border-subtle)" }}
        >
          <MentionInput
            value={intent}
            onChange={setIntent}
            suggestions={suggestions}
            placeholder="Make drawer.tsx use vaul and match our brand… (use @ to reference files or members)"
          />

          <div className="mt-3 flex flex-wrap items-end gap-3">
            <label className="flex flex-col text-xs" style={{ color: "var(--ink-muted)" }}>
              Repo
              <RepoPicker value={repo} onChange={setRepo} className="mt-1 w-56" />
            </label>

            <label className="flex flex-col text-xs" style={{ color: "var(--ink-muted)" }}>
              Team
              <select
                value={teamId}
                onChange={(e) => {
                  setTeamId(e.target.value);
                  setCrewId("");
                }}
                className="mt-1 w-44 rounded-md border px-2 py-1 text-sm"
                style={{
                  background: "var(--surface)",
                  borderColor: "var(--border-subtle)",
                  color: "var(--ink-strong)",
                }}
              >
                <option value="">— none —</option>
                {teams.map((t) => (
                  <option key={t.id} value={t.id}>
                    {t.name}
                  </option>
                ))}
              </select>
            </label>

            <label className="flex flex-col text-xs" style={{ color: "var(--ink-muted)" }}>
              <span className="inline-flex items-center gap-1">
                Crew
                <HelpPopover term="crew" />
              </span>
              <select
                value={crewId}
                onChange={(e) => setCrewId(e.target.value)}
                className="mt-1 w-48 rounded-md border px-2 py-1 text-sm"
                style={{
                  background: "var(--surface)",
                  borderColor: "var(--border-subtle)",
                  color: "var(--ink-strong)",
                }}
              >
                <option value="">— dynamic —</option>
                {crews.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.display_name || c.name}
                  </option>
                ))}
              </select>
            </label>

            <div className="flex flex-col text-xs" style={{ color: "var(--ink-muted)" }}>
              Attachments
              <div className="mt-1">
                <AttachmentUpload attachments={attachments} onChange={setAttachments} />
              </div>
            </div>

            <button
              type="button"
              onClick={run}
              disabled={running || !blueprint}
              className="btn-primary ml-auto !py-2"
            >
              <Send size={15} />
              {running ? "Running…" : "Run"}
            </button>
          </div>

          {error && (
            <p className="mt-2 text-xs" style={{ color: "var(--error)" }}>
              {error}
            </p>
          )}
        </div>

        {/* Blueprint picker (DASH-6) */}
        <div
          className="rounded-xl border p-4"
          style={{ background: "var(--surface)", borderColor: "var(--border-subtle)" }}
        >
          <BlueprintPicker
            blueprints={blueprints}
            value={blueprint}
            onChange={setBlueprint}
            recommended="crew-task"
            loading={loadingBlueprints}
            loadGraph={(name) => api.getBlueprintGraph(name)}
          />
        </div>
      </div>

      {/* Live work area — only mounts once a run is dispatched, so the page
          isn't a giant empty terminal void before you hit Run. */}
      <div className="mt-5 grid grid-cols-1 gap-4 lg:grid-cols-[2fr_1fr]">
        {/* Left: live terminal while running, friendly placeholder while idle. */}
        <div className="flex min-w-0 flex-col">
          {taskId ? (
            <>
              <div className="mb-2 flex items-center justify-between gap-2">
                <div className="flex items-center gap-2">
                  {runState && <RunStateBadge state={runState} />}
                  {taskId && (
                    <Link
                      href={`/runs/${taskId}`}
                      className="inline-flex items-center gap-1 text-[11px] font-medium hover:underline"
                      style={{ color: "var(--accent)" }}
                    >
                      Open full run <ArrowUpRight className="w-3 h-3" />
                    </Link>
                  )}
                </div>
                <span
                  className="inline-flex items-center gap-1.5 text-[11px]"
                  style={{ color: "var(--ink-muted)" }}
                  title={
                    conn === "open"
                      ? "Live — streaming run events"
                      : conn === "closed"
                        ? "Reconnecting — missed frames will backfill"
                        : "Connecting to the event stream"
                  }
                >
                  {conn === "open" ? (
                    <Wifi className="w-3.5 h-3.5" style={{ color: "var(--ok)" }} aria-hidden />
                  ) : (
                    <WifiOff className="w-3.5 h-3.5" aria-hidden />
                  )}
                  {conn === "open" ? "Live" : conn === "closed" ? "Reconnecting…" : "Connecting…"}
                </span>
              </div>
              <div className="h-[420px]">
                <TerminalPanel events={events} />
              </div>
            </>
          ) : (
            <div
              className="flex flex-col items-center justify-center rounded-xl border border-dashed px-6 py-12 text-center"
              style={{ borderColor: "var(--border-subtle)", background: "var(--surface)" }}
            >
              <span
                className="mb-3 inline-flex h-10 w-10 items-center justify-center rounded-full"
                style={{ background: "var(--surface-muted)" }}
              >
                <Terminal className="h-5 w-5" style={{ color: "var(--ink-muted)" }} />
              </span>
              <p className="text-sm font-medium" style={{ color: "var(--ink-strong)" }}>
                Ready when you are
              </p>
              <p className="mt-1 max-w-md text-xs leading-relaxed" style={{ color: "var(--ink-muted)" }}>
                Hit <span className="font-medium" style={{ color: "var(--ink-strong)" }}>Run</span> to
                dispatch the crew. The live terminal and per-stage progress stream in here as it
                builds — open the full run for logs, the developed repo, and the agent timeline.
              </p>
            </div>
          )}
        </div>

        {/* Right: crew preview (idle or running) + checkpoints (running only). */}
        <div className="space-y-4">
          {selectedCrew ? (
            <div
              className="rounded-lg border p-4"
              style={{ background: "var(--surface)", borderColor: "var(--border-subtle)" }}
            >
              <div className="mb-2 flex items-center gap-2">
                <Users size={15} style={{ color: "var(--ink-muted)" }} />
                <h3 className="text-sm font-semibold" style={{ color: "var(--ink-strong)" }}>
                  {selectedCrew.display_name || selectedCrew.name}
                </h3>
              </div>
              <ul className="space-y-1 text-xs">
                {selectedCrew.members.map((m) => (
                  <li key={m.specialization} className="flex items-center justify-between gap-2">
                    <span style={{ color: "var(--ink-strong)" }}>
                      {m.role_label || m.specialization}
                    </span>
                    {m.specialization === selectedCrew.lead && (
                      <span className="pill pill-ok !text-[9px]">lead</span>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          ) : (
            !taskId && (
              <div
                className="rounded-lg border border-dashed p-4 text-xs leading-relaxed"
                style={{ borderColor: "var(--border-subtle)", color: "var(--ink-muted)" }}
              >
                Pick a <span className="font-medium" style={{ color: "var(--ink-strong)" }}>Team</span> +{" "}
                <span className="font-medium" style={{ color: "var(--ink-strong)" }}>Crew</span> to
                preview its members here, or leave Crew on{" "}
                <span className="font-mono">dynamic</span> to let the lead assemble one.
              </div>
            )
          )}
          {taskId && (
            <CheckpointTimeline checkpoints={checkpoints} onRollback={rollback} />
          )}
        </div>
      </div>
    </div>
  );
}

/**
 * Replay-safe append: the SSE stream replays recent frames on reconnect, so the
 * same (task_id, timestamp, stage, phase) frame can arrive twice. Drop the
 * duplicate so the terminal doesn't double-print backfilled lines.
 */
function dedupeAppend(prev: StreamEvent[], next: StreamEvent): StreamEvent[] {
  const key = (e: StreamEvent) =>
    `${e.timestamp}|${e.stage ?? ""}|${e.phase ?? ""}|${e.message ?? ""}`;
  const k = key(next);
  // Only scan the tail — frames arrive in order, so a recent dupe is near the end.
  const tail = prev.slice(-SSE_REPLAY);
  if (tail.some((e) => key(e) === k)) return prev;
  return [...prev, next];
}
