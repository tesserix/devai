"use client";

import { useCallback, useEffect, useState } from "react";
import { Send, Users } from "lucide-react";
import { api, type Crew, type StreamEvent, type Team, type ContextRef } from "@/lib/api";
import { MentionInput, type MentionSuggestion } from "@/components/mention-input";
import { AttachmentUpload, type Attachment } from "@/components/attachment-upload";
import { TerminalPanel } from "@/components/terminal-panel";
import { CheckpointTimeline, type Checkpoint } from "@/components/checkpoint-timeline";

/**
 * Cursor-style composer. Type an intent, @-mention files/teammates, attach
 * images, pick a team → crew, hit Run, and watch the terminal stream + the
 * checkpoint timeline fill in live as the crew works.
 */
export default function ComposePage() {
  const [intent, setIntent] = useState("");
  const [repo, setRepo] = useState("");
  const [blueprint, setBlueprint] = useState("crew-task");

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
  const [error, setError] = useState<string | null>(null);

  // Load teams once.
  useEffect(() => {
    api.listTeams().then(setTeams).catch(() => setTeams([]));
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

  // The selected crew's members become @-mention suggestions (members), plus
  // a couple of file hints once a repo is set (kept light here).
  useEffect(() => {
    const crew = crews.find((c) => c.id === crewId);
    const members: MentionSuggestion[] = (crew?.members || []).map((m) => ({
      value: m.specialization,
      hint: m.role_label || "member",
      kind: "member",
    }));
    setSuggestions(members);
  }, [crews, crewId]);

  // Subscribe to the live event stream once a task is dispatched.
  useEffect(() => {
    if (!taskId) return;
    const es = new EventSource(`/api/pipeline/events/stream`);
    es.addEventListener("stage", (e) => {
      try {
        const data = JSON.parse((e as MessageEvent).data) as StreamEvent;
        if (data.task_id !== taskId) return;
        setEvents((prev) => [...prev, data]);
        if (data.checkpoint) {
          setCheckpoints((prev) => [
            ...prev,
            { sha: data.checkpoint as string, label: data.message || data.stage || "checkpoint", stage: data.stage, ts: data.timestamp },
          ]);
        }
        if (data.phase === "completed" && (data.stage === "post-report" || data.type === "terminal")) {
          // best-effort: stop the spinner when the pipeline reaches its end
          setRunning(false);
        }
      } catch {
        /* ignore malformed frames */
      }
    });
    es.onerror = () => {
      /* EventSource auto-reconnects; nothing to do */
    };
    return () => es.close();
  }, [taskId]);

  const parseContextRefs = useCallback((text: string): ContextRef[] => {
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
  }, [suggestions]);

  async function run() {
    if (!intent.trim() || !repo.trim()) {
      setError("intent and repo are required");
      return;
    }
    setError(null);
    setRunning(true);
    setEvents([]);
    setCheckpoints([]);
    try {
      const result = await api.dispatchCompose({
        intent,
        repo,
        blueprint,
        team_id: teamId || undefined,
        crew_id: crewId || undefined,
        context_refs: parseContextRefs(intent),
        // Pass image file names as object-store keys; the upload endpoint
        // (Phase 5) replaces this with real uploaded keys.
        attachments: attachments.map((a) => a.file.name),
        label: "composer",
      });
      setTaskId(result.task_id);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setRunning(false);
    }
  }

  function rollback(sha: string) {
    if (!taskId) return;
    api.rollbackTo(taskId, sha).catch((e) => setError(String(e)));
  }

  const selectedCrew = crews.find((c) => c.id === crewId);

  return (
    <div className="mx-auto max-w-6xl p-6">
      <header className="mb-5">
        <h1 className="text-xl font-semibold" style={{ color: "var(--text-strong)" }}>
          Compose
        </h1>
        <p className="text-sm" style={{ color: "var(--text-muted)" }}>
          Point a crew at a task — @-mention files, attach mockups, watch it build.
        </p>
      </header>

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
          <label className="flex flex-col text-xs" style={{ color: "var(--text-muted)" }}>
            Repo
            <input
              value={repo}
              onChange={(e) => setRepo(e.target.value)}
              placeholder="tesserix/my-repo"
              className="mt-1 w-56 rounded-md border px-2 py-1 text-sm"
              style={{ background: "var(--surface)", borderColor: "var(--border-subtle)", color: "var(--text-strong)" }}
            />
          </label>

          <label className="flex flex-col text-xs" style={{ color: "var(--text-muted)" }}>
            Team
            <select
              value={teamId}
              onChange={(e) => {
                setTeamId(e.target.value);
                setCrewId("");
              }}
              className="mt-1 w-44 rounded-md border px-2 py-1 text-sm"
              style={{ background: "var(--surface)", borderColor: "var(--border-subtle)", color: "var(--text-strong)" }}
            >
              <option value="">— none —</option>
              {teams.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.name}
                </option>
              ))}
            </select>
          </label>

          <label className="flex flex-col text-xs" style={{ color: "var(--text-muted)" }}>
            Crew
            <select
              value={crewId}
              onChange={(e) => setCrewId(e.target.value)}
              className="mt-1 w-48 rounded-md border px-2 py-1 text-sm"
              style={{ background: "var(--surface)", borderColor: "var(--border-subtle)", color: "var(--text-strong)" }}
            >
              <option value="">— dynamic —</option>
              {crews.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.display_name || c.name}
                </option>
              ))}
            </select>
          </label>

          <div className="flex flex-col text-xs" style={{ color: "var(--text-muted)" }}>
            Attachments
            <div className="mt-1">
              <AttachmentUpload attachments={attachments} onChange={setAttachments} />
            </div>
          </div>

          <button
            type="button"
            onClick={run}
            disabled={running}
            className="ml-auto flex items-center gap-2 rounded-lg px-4 py-2 text-sm font-medium text-white transition disabled:opacity-50"
            style={{ background: "var(--accent, #f97316)" }}
          >
            <Send size={15} />
            {running ? "Running…" : "Run"}
          </button>
        </div>

        {error && (
          <p className="mt-2 text-xs" style={{ color: "#ef4444" }}>
            {error}
          </p>
        )}
      </div>

      {/* Live work area */}
      <div className="mt-5 grid grid-cols-1 gap-4 lg:grid-cols-[2fr_1fr]">
        <div className="h-[420px]">
          <TerminalPanel events={events} />
        </div>
        <div className="space-y-4">
          {selectedCrew && (
            <div
              className="rounded-lg border p-4"
              style={{ background: "var(--surface)", borderColor: "var(--border-subtle)" }}
            >
              <div className="mb-2 flex items-center gap-2">
                <Users size={15} style={{ color: "var(--text-muted)" }} />
                <h3 className="text-sm font-semibold" style={{ color: "var(--text-strong)" }}>
                  {selectedCrew.display_name || selectedCrew.name}
                </h3>
              </div>
              <ul className="space-y-1 text-xs">
                {selectedCrew.members.map((m) => (
                  <li key={m.specialization} className="flex items-center justify-between gap-2">
                    <span style={{ color: "var(--text-strong)" }}>{m.role_label || m.specialization}</span>
                    {m.specialization === selectedCrew.lead && (
                      <span className="rounded px-1.5 py-0.5 text-[10px]" style={{ background: "var(--accent, #f97316)", color: "#fff" }}>
                        lead
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          )}
          <CheckpointTimeline checkpoints={checkpoints} onRollback={taskId ? rollback : undefined} />
        </div>
      </div>
    </div>
  );
}
