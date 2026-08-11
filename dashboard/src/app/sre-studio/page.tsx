"use client";

// SRE Studio — author custom SRE blueprints & agents, dry-run them against
// the live cluster (no side effects), then publish to the shared registry
// for the SRE runtime to schedule and run.

import { useCallback, useEffect, useMemo, useState } from "react";
import { api, type SREDraft, type SREDryRunPreview } from "@/lib/api";
import { useConfirm } from "@/components/confirm-dialog";
import { GuidancePanel } from "@/components/guidance";

const BLUEPRINT_TEMPLATE = `name: my-sre-sweep
description: >-
  What this sweep does.
metadata:
  domain: sre
  kind: sequential
  title: My SRE Sweep
  cadence: hourly
  lanes: [discovery, analyse, respond]
stages:
  - name: discover
    type: agentic
    stage: sre_discover
    lane: discovery
    timeout: 5m
  - name: inspect
    type: agentic
    stage: run_specialization
    lane: analyse
    agent: deployment_inspector
    depends_on: [discover]
    timeout: 10m
    config:
      specialization: deployment_inspector
  - name: respond
    type: agentic
    stage: sre_respond
    lane: respond
    depends_on: [inspect]
    on_failure: continue
  - name: learn
    type: deterministic
    stage: sre_learn
    lane: respond
    depends_on: [respond]
    on_failure: continue
`;

const AGENT_TEMPLATE = `name: my_sre_agent
display_name: My SRE Agent
description: >-
  What this agent inspects. Read-only investigation.
category: sre
llm_provider: openai
llm_model: gpt-4.1
temperature: 0.1
allowed_tools:
  - k8s_get_deployments
  - k8s_get_pod_status
  - prom_query
max_turns: 20
timeout: 10m
context_keys:
  - discovery_output
output_key: my_sre_agent_output
handover_schema:
  overall_status:
    type: string
    required: true
  findings:
    type: array
    required: true
  summary:
    type: string
    required: true
risk_level: low
role_color: engineer
system_prompt: |
  You are an SRE specialist. Investigate using your read-only tools and
  report findings as JSON matching the handover schema.
`;

function statusColor(phase: string): string {
  if (phase === "completed") return "var(--ok)";
  if (phase === "failed") return "var(--error)";
  if (phase === "skipped") return "var(--ink-muted)";
  return "var(--accent)";
}

export default function SREStudioPage() {
  const confirm = useConfirm();
  const [drafts, setDrafts] = useState<SREDraft[]>([]);
  const [selectedId, setSelectedId] = useState<string>("");
  const [selected, setSelected] = useState<SREDraft | null>(null);
  const [yamlText, setYamlText] = useState("");
  const [tab, setTab] = useState<"editor" | "dryrun">("editor");
  const [preview, setPreview] = useState<SREDryRunPreview | null>(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [creating, setCreating] = useState(false);
  const [newKind, setNewKind] = useState<"blueprint" | "agent">("blueprint");

  const refresh = useCallback(async () => {
    try {
      setDrafts(await api.sreStudio.listDrafts());
    } catch (e) {
      setError(String((e as Error)?.message ?? e));
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  useEffect(() => {
    if (!selectedId) {
      setSelected(null);
      return;
    }
    let cancelled = false;
    api.sreStudio
      .getDraft(selectedId)
      .then((d) => {
        if (cancelled) return;
        setSelected(d);
        setYamlText(d.yaml);
        setPreview(d.dry_run_summary ?? null);
        setTab("editor");
      })
      .catch((e) => !cancelled && setError(String((e as Error)?.message ?? e)));
    return () => {
      cancelled = true;
    };
  }, [selectedId]);

  const dirty = useMemo(() => selected !== null && yamlText !== selected.yaml, [selected, yamlText]);

  async function createDraft() {
    setError("");
    setBusy("create");
    try {
      const d = await api.sreStudio.createDraft({
        kind: newKind,
        yaml: newKind === "blueprint" ? BLUEPRINT_TEMPLATE : AGENT_TEMPLATE,
      });
      setCreating(false);
      await refresh();
      setSelectedId(d.id);
    } catch (e) {
      setError(String((e as Error)?.message ?? e));
    } finally {
      setBusy("");
    }
  }

  async function save() {
    if (!selected) return;
    setError("");
    setBusy("save");
    try {
      const d = await api.sreStudio.updateDraft(selected.id, { yaml: yamlText });
      setSelected(d);
      await refresh();
    } catch (e) {
      setError(String((e as Error)?.message ?? e));
    } finally {
      setBusy("");
    }
  }

  async function dryRun() {
    if (!selected) return;
    setError("");
    setBusy("dryrun");
    try {
      if (dirty) await api.sreStudio.updateDraft(selected.id, { yaml: yamlText });
      const p = await api.sreStudio.dryRun(selected.id);
      setPreview(p);
      setTab("dryrun");
      await refresh();
    } catch (e) {
      setError(String((e as Error)?.message ?? e));
    } finally {
      setBusy("");
    }
  }

  async function publish() {
    if (!selected) return;
    setError("");
    setBusy("publish");
    try {
      if (dirty) await api.sreStudio.updateDraft(selected.id, { yaml: yamlText });
      await api.sreStudio.publish(selected.id);
      await refresh();
      const d = await api.sreStudio.getDraft(selected.id);
      setSelected(d);
    } catch (e) {
      setError(String((e as Error)?.message ?? e));
    } finally {
      setBusy("");
    }
  }

  async function remove(id: string) {
    const ok = await confirm({
      title: "Delete draft?",
      message: "This permanently removes the SRE Studio draft. This can't be undone.",
      confirmLabel: "Delete",
      tone: "danger",
    });
    if (!ok) return;
    setBusy("delete");
    try {
      await api.sreStudio.deleteDraft(id);
      if (selectedId === id) setSelectedId("");
      await refresh();
    } catch (e) {
      setError(String((e as Error)?.message ?? e));
    } finally {
      setBusy("");
    }
  }

  return (
    <div className="flex h-full" style={{ background: "var(--canvas)" }}>
      {/* Draft list */}
      <aside
        className="w-72 shrink-0 flex flex-col border-r"
        style={{ background: "var(--surface)", borderColor: "var(--border-subtle)" }}
      >
        <div className="px-4 pt-4 pb-2 flex items-center justify-between">
          <span className="label-eyebrow">SRE Studio</span>
          <button className="btn-primary !py-1 !px-2 text-xs" onClick={() => setCreating((v) => !v)}>
            + New
          </button>
        </div>
        {creating && (
          <div className="mx-3 mb-2 p-3 rounded-lg" style={{ background: "var(--surface-muted)" }}>
            <div className="flex gap-1 mb-2">
              {(["blueprint", "agent"] as const).map((k) => (
                <button
                  key={k}
                  onClick={() => setNewKind(k)}
                  className="flex-1 text-xs py-1 rounded border"
                  style={{
                    background: newKind === k ? "var(--accent-soft-bg)" : "transparent",
                    color: newKind === k ? "var(--accent-soft-ink)" : "var(--ink-soft)",
                    borderColor: "var(--border)",
                  }}
                >
                  {k}
                </button>
              ))}
            </div>
            <button className="btn-primary w-full !py-1.5 text-xs" disabled={busy === "create"} onClick={createDraft}>
              {busy === "create" ? "Creating…" : `Create ${newKind} draft`}
            </button>
          </div>
        )}
        <div className="flex-1 overflow-y-auto px-2 pb-3">
          {drafts.length === 0 ? (
            <div className="text-xs text-center py-12" style={{ color: "var(--ink-soft)" }}>
              No drafts yet
            </div>
          ) : (
            <ul className="space-y-0.5">
              {drafts.map((d) => (
                <li key={d.id}>
                  <button
                    onClick={() => setSelectedId(d.id)}
                    className="w-full text-left px-2 py-2 rounded transition-colors"
                    style={{
                      background: selectedId === d.id ? "var(--surface-hover)" : "transparent",
                    }}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span
                        className="text-sm font-medium truncate"
                        style={{ color: "var(--ink-strong)" }}
                      >
                        {d.name}
                      </span>
                      <span
                        className={d.status === "published" ? "pill pill-ok" : "pill"}
                        style={{ fontSize: 9 }}
                      >
                        {d.status}
                      </span>
                    </div>
                    <span className="text-xs" style={{ color: "var(--ink-muted)" }}>
                      {d.kind}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      </aside>

      {/* Editor / results */}
      <main className="flex-1 flex flex-col overflow-hidden">
        {error && (
          <div className="px-6 py-2 text-sm" style={{ background: "var(--error)", color: "var(--ink-inverse)" }}>
            {error}
          </div>
        )}
        {!selected ? (
          <div className="flex-1 flex items-center justify-center" style={{ color: "var(--ink-soft)" }}>
            Select or create a draft to begin
          </div>
        ) : (
          <>
            <header
              className="border-b px-6 py-3 shrink-0 flex items-center justify-between gap-4"
              style={{ background: "var(--surface)", borderColor: "var(--border-subtle)" }}
            >
              <div className="min-w-0">
                <h2 className="text-sm font-semibold truncate" style={{ color: "var(--ink-strong)" }}>
                  {selected.name}{" "}
                  <span className="text-xs font-normal" style={{ color: "var(--ink-muted)" }}>
                    · {selected.kind} · {selected.status}
                  </span>
                </h2>
              </div>
              <div className="flex items-center gap-2 shrink-0">
                <button className="btn-secondary !py-1.5 text-xs" disabled={!dirty || busy !== ""} onClick={save}>
                  {busy === "save" ? "Saving…" : dirty ? "Save" : "Saved"}
                </button>
                {selected.kind === "blueprint" && (
                  <button className="btn-secondary !py-1.5 text-xs" disabled={busy !== ""} onClick={dryRun}>
                    {busy === "dryrun" ? "Running…" : "Dry run"}
                  </button>
                )}
                <button className="btn-primary !py-1.5 text-xs" disabled={busy !== ""} onClick={publish}>
                  {busy === "publish" ? "Publishing…" : "Publish"}
                </button>
                <button
                  className="btn-ghost !py-1.5 text-xs"
                  style={{ color: "var(--error)" }}
                  disabled={busy !== ""}
                  onClick={() => remove(selected.id)}
                >
                  Delete
                </button>
              </div>
            </header>

            <div className="border-b px-6 shrink-0" style={{ borderColor: "var(--border-subtle)" }}>
              <div className="flex gap-0 -mb-px">
                {([
                  { key: "editor", label: "Editor" },
                  { key: "dryrun", label: "Dry-run results" },
                ] as const).map((t) => (
                  <button
                    key={t.key}
                    onClick={() => setTab(t.key)}
                    className="px-3.5 py-2.5 text-sm font-medium border-b-2 transition-colors"
                    style={{
                      borderColor: tab === t.key ? "var(--accent)" : "transparent",
                      color: tab === t.key ? "var(--accent-soft-ink)" : "var(--ink-soft)",
                    }}
                  >
                    {t.label}
                  </button>
                ))}
              </div>
            </div>

            <div className="flex-1 overflow-y-auto p-6 space-y-4">
              <GuidancePanel id="sre-studio" />
              {tab === "editor" ? (
                <textarea
                  value={yamlText}
                  onChange={(e) => setYamlText(e.target.value)}
                  spellCheck={false}
                  className="w-full h-full min-h-[420px] font-mono text-xs p-4 rounded-lg resize-none"
                  style={{
                    background: "var(--surface-raised)",
                    color: "var(--ink)",
                    border: "1px solid var(--border)",
                  }}
                />
              ) : (
                <DryRunView preview={preview} />
              )}
            </div>
          </>
        )}
      </main>
    </div>
  );
}

function DryRunView({ preview }: { preview: SREDryRunPreview | null }) {
  if (!preview) {
    return (
      <div className="text-sm" style={{ color: "var(--ink-soft)" }}>
        No dry-run yet. Hit <strong>Dry run</strong> to preview what this config would do — live findings, zero side
        effects.
      </div>
    );
  }
  return (
    <div className="space-y-4">
      <div className="panel p-4">
        <div className="flex items-center justify-between">
          <span className="text-sm font-semibold" style={{ color: "var(--ink-strong)" }}>
            {preview.blueprint}
          </span>
          <span className="pill">{preview.state}</span>
        </div>
        <p className="text-xs mt-1" style={{ color: "var(--ink-muted)" }}>
          {preview.note}
        </p>
      </div>

      <div className="panel p-4">
        <div className="label-eyebrow mb-2">Stage timeline</div>
        <ul className="space-y-1">
          {preview.stage_events
            .filter((e) => e.phase !== "started")
            .map((e, i) => (
              <li key={i} className="flex items-center gap-3 text-sm">
                <span className="dot" style={{ background: statusColor(e.phase) }} />
                <span className="font-mono text-xs" style={{ color: "var(--ink)" }}>
                  {e.stage}
                </span>
                <span className="text-xs" style={{ color: "var(--ink-muted)" }}>
                  {e.phase}
                  {e.duration_ms != null ? ` · ${(e.duration_ms / 1000).toFixed(1)}s` : ""}
                </span>
                {e.error && (
                  <span className="text-xs" style={{ color: "var(--error)" }}>
                    {e.error}
                  </span>
                )}
              </li>
            ))}
        </ul>
      </div>

      <div className="panel p-4">
        <div className="label-eyebrow mb-2">Outputs (what each role produced)</div>
        <pre
          className="text-xs overflow-x-auto p-3 rounded"
          style={{ background: "var(--surface-raised)", color: "var(--ink)" }}
        >
          {JSON.stringify(preview.outputs, null, 2)}
        </pre>
      </div>
    </div>
  );
}
