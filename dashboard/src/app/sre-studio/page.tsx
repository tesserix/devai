"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertCircle,
  Bot,
  Braces,
  CheckCircle2,
  CircleDashed,
  Clock3,
  FileCode2,
  FlaskConical,
  Layers3,
  LoaderCircle,
  Plus,
  Rocket,
  Search,
  ShieldCheck,
  SkipForward,
  Trash2,
  Workflow,
  X,
} from "lucide-react";
import { api, type SREDraft, type SREDryRunPreview } from "@/lib/api";
import { useConfirm } from "@/components/confirm-dialog";
import { GuidanceInfo, GuidancePanel } from "@/components/guidance";
import {
  filterStudioDrafts,
  formatDuration,
  persistStudioDraft,
  summarizeStageEvents,
} from "@/lib/sre-studio";

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

function statusPillClass(status: string): string {
  if (["completed", "published", "succeeded", "success"].includes(status)) return "pill pill-ok";
  if (["failed", "error"].includes(status)) return "pill pill-error";
  if (["running", "started"].includes(status)) return "pill pill-info";
  if (status === "skipped") return "pill pill-muted";
  return "pill pill-warn";
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
  const [loadingDrafts, setLoadingDrafts] = useState(true);
  const [loadingSelected, setLoadingSelected] = useState(false);
  const [creating, setCreating] = useState(false);
  const [newKind, setNewKind] = useState<"blueprint" | "agent">("blueprint");
  const [query, setQuery] = useState("");

  const refresh = useCallback(async () => {
    try {
      setError("");
      setDrafts(await api.sreStudio.listDrafts());
    } catch (e) {
      setError(String((e as Error)?.message ?? e));
    } finally {
      setLoadingDrafts(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  useEffect(() => {
    if (!selectedId) {
      setSelected(null);
      setLoadingSelected(false);
      return;
    }
    let cancelled = false;
    setError("");
    setSelected(null);
    setLoadingSelected(true);
    api.sreStudio
      .getDraft(selectedId)
      .then((d) => {
        if (cancelled) return;
        setSelected(d);
        setYamlText(d.yaml);
        setPreview(d.dry_run_summary ?? null);
        setTab("editor");
      })
      .catch((e) => !cancelled && setError(String((e as Error)?.message ?? e)))
      .finally(() => {
        if (!cancelled) setLoadingSelected(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedId]);

  const dirty = useMemo(() => selected !== null && yamlText !== selected.yaml, [selected, yamlText]);
  const visibleDrafts = filterStudioDrafts(drafts, query);
  const publishedCount = drafts.filter((draft) => draft.status === "published").length;
  const lineCount = yamlText.split("\n").length;

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
      const saved = await persistStudioDraft(selected, yamlText, api.sreStudio.updateDraft);
      const p = await api.sreStudio.dryRun(selected.id);
      setSelected({ ...saved, dry_run_summary: p });
      setYamlText(saved.yaml);
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
      await persistStudioDraft(selected, yamlText, api.sreStudio.updateDraft);
      await api.sreStudio.publish(selected.id);
      await refresh();
      const d = await api.sreStudio.getDraft(selected.id);
      setSelected(d);
      setYamlText(d.yaml);
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
    <div
      className="flex min-h-full flex-col lg:h-full lg:min-h-0 lg:flex-row lg:overflow-hidden"
      style={{ background: "var(--canvas)" }}
    >
      <aside
        className="flex max-h-[32rem] min-h-[22rem] w-full shrink-0 flex-col border-b lg:h-full lg:max-h-none lg:min-h-0 lg:w-[21rem] lg:border-r lg:border-b-0"
        style={{ background: "var(--surface)", borderColor: "var(--border-subtle)" }}
      >
        <div className="border-b p-4 sm:p-5" style={{ borderColor: "var(--border-subtle)" }}>
          <div className="flex items-start justify-between gap-3">
            <div className="flex min-w-0 items-center gap-3">
              <span
                className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl"
                style={{ background: "var(--accent-soft-bg)", color: "var(--accent-soft-ink)" }}
              >
                <Workflow className="h-5 w-5" />
              </span>
              <div className="min-w-0">
                <p className="label-eyebrow mb-0.5">Reliability workspace</p>
                <h1 className="font-serif text-xl leading-tight" style={{ color: "var(--ink-strong)" }}>
                  SRE Studio
                </h1>
              </div>
            </div>
            <button
              type="button"
              className="btn-primary !px-3 !py-2"
              onClick={() => setCreating((value) => !value)}
              aria-expanded={creating}
            >
              {creating ? <X className="h-4 w-4" /> : <Plus className="h-4 w-4" />}
              {creating ? "Close" : "New draft"}
            </button>
          </div>

          <p className="mt-3 text-xs leading-5" style={{ color: "var(--ink-soft)" }}>
            Author, simulate, and publish safe SRE automation from one workspace.
          </p>

          <div className="mt-4 grid grid-cols-2 gap-2">
            <div className="panel-muted px-3 py-2.5">
              <p className="text-lg font-semibold leading-none" style={{ color: "var(--ink-strong)" }}>
                {drafts.length}
              </p>
              <p className="mt-1 text-[11px]" style={{ color: "var(--ink-muted)" }}>
                Total drafts
              </p>
            </div>
            <div className="panel-muted px-3 py-2.5">
              <p className="text-lg font-semibold leading-none" style={{ color: "var(--ink-strong)" }}>
                {publishedCount}
              </p>
              <p className="mt-1 text-[11px]" style={{ color: "var(--ink-muted)" }}>
                Published
              </p>
            </div>
          </div>

          <label className="relative mt-3 block">
            <span className="sr-only">Search SRE Studio drafts</span>
            <Search
              className="pointer-events-none absolute top-1/2 left-3 h-4 w-4 -translate-y-1/2"
              style={{ color: "var(--ink-muted)" }}
            />
            <input
              className="field w-full !py-2 !pr-3 !pl-9 text-sm"
              type="search"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Search drafts"
            />
          </label>
        </div>

        {creating && (
          <div
            className="border-b p-4"
            style={{ background: "var(--surface-muted)", borderColor: "var(--border-subtle)" }}
          >
            <div className="mb-3">
              <p className="text-sm font-semibold" style={{ color: "var(--ink-strong)" }}>
                Choose a starting point
              </p>
              <p className="mt-0.5 text-xs" style={{ color: "var(--ink-muted)" }}>
                Start with a safe, editable template.
              </p>
            </div>
            <div className="grid grid-cols-2 gap-2" role="group" aria-label="Draft type">
              <button
                type="button"
                aria-pressed={newKind === "blueprint"}
                onClick={() => setNewKind("blueprint")}
                className="rounded-lg border p-3 text-left transition-colors"
                style={{
                  background: newKind === "blueprint" ? "var(--accent-soft-bg)" : "var(--surface)",
                  borderColor: newKind === "blueprint" ? "var(--accent-soft-bd)" : "var(--border)",
                }}
              >
                <Workflow className="mb-2 h-4 w-4" style={{ color: "var(--accent-soft-ink)" }} />
                <span className="block text-xs font-semibold" style={{ color: "var(--ink-strong)" }}>
                  Blueprint
                </span>
                <span className="mt-1 block text-[10px] leading-4" style={{ color: "var(--ink-muted)" }}>
                  Multi-stage workflow
                </span>
              </button>
              <button
                type="button"
                aria-pressed={newKind === "agent"}
                onClick={() => setNewKind("agent")}
                className="rounded-lg border p-3 text-left transition-colors"
                style={{
                  background: newKind === "agent" ? "var(--accent-soft-bg)" : "var(--surface)",
                  borderColor: newKind === "agent" ? "var(--accent-soft-bd)" : "var(--border)",
                }}
              >
                <Bot className="mb-2 h-4 w-4" style={{ color: "var(--accent-soft-ink)" }} />
                <span className="block text-xs font-semibold" style={{ color: "var(--ink-strong)" }}>
                  Specialist
                </span>
                <span className="mt-1 block text-[10px] leading-4" style={{ color: "var(--ink-muted)" }}>
                  Focused SRE agent
                </span>
              </button>
            </div>
            <button
              type="button"
              className="btn-primary mt-3 w-full"
              disabled={busy === "create"}
              onClick={createDraft}
            >
              {busy === "create" ? (
                <LoaderCircle className="h-4 w-4 animate-spin motion-reduce:animate-none" />
              ) : (
                <Plus className="h-4 w-4" />
              )}
              {busy === "create" ? "Creating draft…" : `Create ${newKind}`}
            </button>
          </div>
        )}

        <div className="flex min-h-0 flex-1 flex-col">
          <div className="flex items-center justify-between px-4 pt-3 pb-2">
            <span className="label-eyebrow">Draft library</span>
            {!loadingDrafts && (
              <span className="text-[11px]" style={{ color: "var(--ink-muted)" }}>
                {visibleDrafts.length} {visibleDrafts.length === 1 ? "item" : "items"}
              </span>
            )}
          </div>
          <nav className="min-h-0 flex-1 overflow-y-auto px-2 pb-3" aria-label="SRE Studio drafts">
            {loadingDrafts ? (
              <div className="space-y-2 px-1" aria-label="Loading drafts" aria-busy="true">
                {[0, 1, 2].map((item) => (
                  <div
                    key={item}
                    className="h-[78px] animate-pulse rounded-xl motion-reduce:animate-none"
                    style={{ background: "var(--surface-muted)" }}
                  />
                ))}
              </div>
            ) : visibleDrafts.length === 0 ? (
              <div className="flex flex-col items-center px-5 py-8 text-center">
                <span
                  className="mb-3 flex h-10 w-10 items-center justify-center rounded-full"
                  style={{ background: "var(--surface-muted)", color: "var(--ink-muted)" }}
                >
                  {query ? <Search className="h-4 w-4" /> : <Layers3 className="h-4 w-4" />}
                </span>
                <p className="text-sm font-medium" style={{ color: "var(--ink-strong)" }}>
                  {query ? "No matching drafts" : "No drafts yet"}
                </p>
                <p className="mt-1 text-xs leading-5" style={{ color: "var(--ink-muted)" }}>
                  {query ? "Try a different name, type, or status." : "Create your first reliability workflow."}
                </p>
                {query ? (
                  <button type="button" className="btn-ghost mt-2 !py-1.5 text-xs" onClick={() => setQuery("")}>
                    Clear search
                  </button>
                ) : (
                  <button type="button" className="btn-secondary mt-3 !py-1.5 text-xs" onClick={() => setCreating(true)}>
                    <Plus className="h-3.5 w-3.5" />
                    New draft
                  </button>
                )}
              </div>
            ) : (
              <ul className="space-y-1.5">
                {visibleDrafts.map((draft) => {
                  const active = selectedId === draft.id;
                  return (
                    <li key={draft.id}>
                      <button
                        type="button"
                        onClick={() => setSelectedId(draft.id)}
                        className="w-full rounded-xl border px-3 py-3 text-left transition-colors"
                        style={{
                          background: active ? "var(--accent-soft-bg)" : "transparent",
                          borderColor: active ? "var(--accent-soft-bd)" : "transparent",
                        }}
                        aria-current={active ? "page" : undefined}
                      >
                        <div className="flex items-start gap-3">
                          <span
                            className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg"
                            style={{
                              background: active ? "var(--surface)" : "var(--surface-muted)",
                              color: active ? "var(--accent-soft-ink)" : "var(--ink-soft)",
                            }}
                          >
                            {draft.kind === "blueprint" ? (
                              <Workflow className="h-4 w-4" />
                            ) : (
                              <Bot className="h-4 w-4" />
                            )}
                          </span>
                          <span className="min-w-0 flex-1">
                            <span className="block truncate text-sm font-semibold" style={{ color: "var(--ink-strong)" }}>
                              {draft.name}
                            </span>
                            <span
                              className="mt-1 block line-clamp-2 text-[11px] leading-4"
                              style={{ color: "var(--ink-muted)" }}
                            >
                              {draft.description || "No description yet"}
                            </span>
                          </span>
                        </div>
                        <span className="mt-2.5 flex items-center justify-between pl-11">
                          <span className="text-[10px] font-medium capitalize" style={{ color: "var(--ink-soft)" }}>
                            {draft.kind}
                          </span>
                          <span className={draft.status === "published" ? "pill pill-ok" : "pill pill-muted"}>
                            {draft.status}
                          </span>
                        </span>
                      </button>
                    </li>
                  );
                })}
              </ul>
            )}
          </nav>
        </div>
      </aside>

      <main className="flex min-h-[38rem] min-w-0 flex-1 flex-col overflow-hidden" aria-busy={loadingSelected}>
        {error && (
          <div
            className="flex items-start gap-3 border-b px-4 py-3 text-sm sm:px-6"
            style={{ background: "var(--error-soft-bg)", borderColor: "var(--error-soft-bd)", color: "var(--error-ink)" }}
            role="alert"
          >
            <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
            <span className="min-w-0 flex-1">{error}</span>
            <button type="button" className="shrink-0 rounded p-0.5" onClick={() => setError("")} aria-label="Dismiss error">
              <X className="h-4 w-4" />
            </button>
          </div>
        )}

        {loadingSelected ? (
          <div className="flex-1 animate-pulse p-5 motion-reduce:animate-none sm:p-8" aria-label="Loading draft">
            <div className="mb-8 h-16 rounded-xl" style={{ background: "var(--surface-muted)" }} />
            <div className="mb-4 h-9 w-56 rounded-lg" style={{ background: "var(--surface-muted)" }} />
            <div className="h-[28rem] rounded-xl" style={{ background: "var(--surface-muted)" }} />
          </div>
        ) : !selected ? (
          <div className="flex flex-1 items-center justify-center overflow-y-auto p-5 sm:p-8">
            <section className="w-full max-w-3xl text-center">
              <div className="relative mx-auto mb-6 h-24 w-24">
                <span
                  className="absolute inset-0 rounded-3xl rotate-6"
                  style={{ background: "var(--accent-soft-bg-2)" }}
                />
                <span
                  className="absolute inset-2 flex items-center justify-center rounded-2xl shadow-sm"
                  style={{ background: "var(--surface-raised)", color: "var(--accent-soft-ink)" }}
                >
                  <ShieldCheck className="h-10 w-10" />
                </span>
              </div>
              <p className="label-eyebrow mb-2">Safe automation lifecycle</p>
              <h2 className="font-serif text-3xl tracking-tight sm:text-4xl" style={{ color: "var(--ink-strong)" }}>
                Build reliability workflows with confidence
              </h2>
              <p className="mx-auto mt-3 max-w-xl text-sm leading-6" style={{ color: "var(--ink-soft)" }}>
                Turn operational knowledge into versioned blueprints, validate every stage against live signals, then publish when the result is ready.
              </p>

              <div className="mt-8 grid gap-3 text-left sm:grid-cols-3">
                {[
                  { icon: FileCode2, title: "Author", copy: "Define stages and guardrails in readable YAML." },
                  { icon: FlaskConical, title: "Simulate", copy: "Inspect live findings with zero side effects." },
                  { icon: Rocket, title: "Publish", copy: "Share approved automation with the SRE runtime." },
                ].map((item) => {
                  const Icon = item.icon;
                  return (
                    <div key={item.title} className="panel p-4">
                      <Icon className="mb-3 h-5 w-5" style={{ color: "var(--accent-soft-ink)" }} />
                      <h3 className="text-sm font-semibold" style={{ color: "var(--ink-strong)" }}>
                        {item.title}
                      </h3>
                      <p className="mt-1 text-xs leading-5" style={{ color: "var(--ink-muted)" }}>
                        {item.copy}
                      </p>
                    </div>
                  );
                })}
              </div>

              <button type="button" className="btn-primary mt-7" onClick={() => setCreating(true)}>
                <Plus className="h-4 w-4" />
                Create a draft
              </button>
            </section>
          </div>
        ) : (
          <>
            <header
              className="shrink-0 border-b px-4 py-4 sm:px-6"
              style={{ background: "var(--surface)", borderColor: "var(--border-subtle)" }}
            >
              <div className="flex flex-col gap-4 xl:flex-row xl:items-center xl:justify-between">
                <div className="flex min-w-0 items-start gap-3">
                  <span
                    className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl"
                    style={{ background: "var(--surface-muted)", color: "var(--accent-soft-ink)" }}
                  >
                    {selected.kind === "blueprint" ? <Workflow className="h-5 w-5" /> : <Bot className="h-5 w-5" />}
                  </span>
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <h2 className="truncate text-lg font-semibold" style={{ color: "var(--ink-strong)" }}>
                        {selected.name}
                      </h2>
                      <span className="pill pill-muted">{selected.kind}</span>
                      <span className={selected.status === "published" ? "pill pill-ok" : "pill pill-warn"}>
                        {selected.status}
                      </span>
                      {dirty && <span className="pill pill-warn">Unsaved</span>}
                    </div>
                    <p className="mt-1 line-clamp-1 text-xs" style={{ color: "var(--ink-muted)" }}>
                      {selected.description || "Edit the YAML definition, validate it, then publish when ready."}
                    </p>
                  </div>
                </div>

                <div className="flex flex-wrap items-center gap-2 xl:justify-end">
                  <button type="button" className="btn-secondary" disabled={!dirty || busy !== ""} onClick={save}>
                    {busy === "save" ? (
                      <LoaderCircle className="h-4 w-4 animate-spin motion-reduce:animate-none" />
                    ) : (
                      <CheckCircle2 className="h-4 w-4" />
                    )}
                    {busy === "save" ? "Saving…" : dirty ? "Save changes" : "Saved"}
                  </button>
                  {selected.kind === "blueprint" && (
                    <button type="button" className="btn-secondary" disabled={busy !== ""} onClick={dryRun}>
                      {busy === "dryrun" ? (
                        <LoaderCircle className="h-4 w-4 animate-spin motion-reduce:animate-none" />
                      ) : (
                        <FlaskConical className="h-4 w-4" />
                      )}
                      {busy === "dryrun" ? "Running…" : "Dry run"}
                    </button>
                  )}
                  <button type="button" className="btn-primary" disabled={busy !== ""} onClick={publish}>
                    {busy === "publish" ? (
                      <LoaderCircle className="h-4 w-4 animate-spin motion-reduce:animate-none" />
                    ) : (
                      <Rocket className="h-4 w-4" />
                    )}
                    {busy === "publish"
                      ? "Publishing…"
                      : selected.status === "published"
                        ? "Publish update"
                        : "Publish"}
                  </button>
                  <button
                    type="button"
                    className="btn-ghost"
                    style={{ color: "var(--error)" }}
                    disabled={busy !== ""}
                    onClick={() => remove(selected.id)}
                  >
                    <Trash2 className="h-4 w-4" />
                    Delete
                  </button>
                </div>
              </div>
            </header>

            <div
              className="shrink-0 border-b px-4 sm:px-6"
              style={{ background: "var(--surface)", borderColor: "var(--border-subtle)" }}
            >
              <div className="flex items-center gap-1 -mb-px" role="tablist" aria-label="Draft workspace">
                <button
                  type="button"
                  id="sre-editor-tab"
                  role="tab"
                  aria-selected={tab === "editor"}
                  aria-controls="sre-editor-panel"
                  onClick={() => setTab("editor")}
                  className="flex items-center gap-2 border-b-2 px-3 py-3 text-sm font-medium transition-colors"
                  style={{
                    borderColor: tab === "editor" ? "var(--accent)" : "transparent",
                    color: tab === "editor" ? "var(--accent-soft-ink)" : "var(--ink-soft)",
                  }}
                >
                  <FileCode2 className="h-4 w-4" />
                  Editor
                </button>
                {selected.kind === "blueprint" && (
                  <button
                    type="button"
                    id="sre-dryrun-tab"
                    role="tab"
                    aria-selected={tab === "dryrun"}
                    aria-controls="sre-dryrun-panel"
                    onClick={() => setTab("dryrun")}
                    className="flex items-center gap-2 border-b-2 px-3 py-3 text-sm font-medium transition-colors"
                    style={{
                      borderColor: tab === "dryrun" ? "var(--accent)" : "transparent",
                      color: tab === "dryrun" ? "var(--accent-soft-ink)" : "var(--ink-soft)",
                    }}
                  >
                    <FlaskConical className="h-4 w-4" />
                    Results
                    {preview && <span className={statusPillClass(preview.state)}>{preview.state}</span>}
                  </button>
                )}
                <GuidanceInfo id="sre-studio" className="order-last ml-auto mr-1" />
              </div>
            </div>

            <div className="flex-1 overflow-y-auto p-4 sm:p-6">
              <div className="mx-auto w-full max-w-[90rem] space-y-4">
                <GuidancePanel id="sre-studio" />
                {tab === "editor" || selected.kind === "agent" ? (
                  <section
                    id="sre-editor-panel"
                    role="tabpanel"
                    aria-labelledby="sre-editor-tab"
                    className="panel-raised overflow-hidden"
                  >
                    <div
                      className="flex flex-wrap items-center justify-between gap-3 border-b px-4 py-3 sm:px-5"
                      style={{ borderColor: "var(--border-subtle)" }}
                    >
                      <div>
                        <h3 className="flex items-center gap-2 text-sm font-semibold" style={{ color: "var(--ink-strong)" }}>
                          <Braces className="h-4 w-4" style={{ color: "var(--accent-soft-ink)" }} />
                          YAML definition
                        </h3>
                        <p id="sre-editor-help" className="mt-1 text-xs" style={{ color: "var(--ink-muted)" }}>
                          Define the workflow contract, limits, and safe execution path.
                        </p>
                      </div>
                      <div className="flex items-center gap-2 text-[11px]" style={{ color: "var(--ink-muted)" }}>
                        <span>{lineCount} lines</span>
                        <span aria-hidden>·</span>
                        <span>{yamlText.length.toLocaleString()} characters</span>
                      </div>
                    </div>
                    <div className="p-3 sm:p-4">
                      <textarea
                        value={yamlText}
                        onChange={(event) => setYamlText(event.target.value)}
                        spellCheck={false}
                        aria-label="SRE draft YAML definition"
                        aria-describedby="sre-editor-help"
                        className="field min-h-[30rem] w-full resize-y !rounded-lg !p-4 font-mono text-[13px] leading-6"
                        style={{ background: "var(--surface-raised)", color: "var(--ink)" }}
                      />
                    </div>
                    <div
                      className="flex flex-wrap items-center justify-between gap-2 border-t px-4 py-2.5 text-[11px] sm:px-5"
                      style={{ background: "var(--surface-muted)", borderColor: "var(--border-subtle)", color: "var(--ink-muted)" }}
                    >
                      <span>Dry runs save current edits before validating a blueprint.</span>
                      <span className={dirty ? "pill pill-warn" : "pill pill-ok"}>{dirty ? "Unsaved changes" : "Up to date"}</span>
                    </div>
                  </section>
                ) : (
                  <section id="sre-dryrun-panel" role="tabpanel" aria-labelledby="sre-dryrun-tab">
                    <DryRunView preview={preview} />
                  </section>
                )}
              </div>
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
      <div className="panel-raised flex flex-col items-center px-5 py-14 text-center sm:px-10">
        <span
          className="mb-4 flex h-14 w-14 items-center justify-center rounded-2xl"
          style={{ background: "var(--accent-soft-bg)", color: "var(--accent-soft-ink)" }}
        >
          <FlaskConical className="h-7 w-7" />
        </span>
        <h3 className="font-serif text-2xl" style={{ color: "var(--ink-strong)" }}>
          No simulation results yet
        </h3>
        <p className="mt-2 max-w-md text-sm leading-6" style={{ color: "var(--ink-soft)" }}>
          Run this blueprint against live signals to inspect its stages and outputs. Dry runs are read-only and make no changes.
        </p>
        <span className="mt-5 inline-flex items-center gap-2 text-xs" style={{ color: "var(--ok-ink)" }}>
          <ShieldCheck className="h-4 w-4" />
          Zero side effects
        </span>
      </div>
    );
  }

  const summary = summarizeStageEvents(preview.stage_events);
  const outputCount = Object.keys(preview.outputs).length;

  return (
    <div className="space-y-4" aria-live="polite">
      <section className="panel-raised overflow-hidden">
        <div className="flex flex-col gap-4 p-5 sm:flex-row sm:items-start sm:justify-between">
          <div className="flex min-w-0 items-start gap-3">
            <span
              className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl"
              style={{ background: "var(--accent-soft-bg)", color: "var(--accent-soft-ink)" }}
            >
              <FlaskConical className="h-5 w-5" />
            </span>
            <div className="min-w-0">
              <p className="label-eyebrow mb-1">Latest dry run</p>
              <h3 className="truncate text-base font-semibold" style={{ color: "var(--ink-strong)" }}>
                {preview.blueprint}
              </h3>
              <p className="mt-1 text-xs" style={{ color: "var(--ink-muted)" }}>
                Task {preview.task_id}
              </p>
            </div>
          </div>
          <span className={statusPillClass(preview.state)}>{preview.state}</span>
        </div>
        <div
          className="grid grid-cols-2 border-t sm:grid-cols-4"
          style={{ borderColor: "var(--border-subtle)", background: "var(--surface-muted)" }}
        >
          {[
            { label: "Completed", value: summary.completed, icon: CheckCircle2, color: "var(--ok)" },
            { label: "Failed", value: summary.failed, icon: AlertCircle, color: "var(--error)" },
            { label: "Skipped", value: summary.skipped, icon: SkipForward, color: "var(--ink-muted)" },
            { label: "Duration", value: formatDuration(summary.totalDurationMs), icon: Clock3, color: "var(--info)" },
          ].map((metric) => {
            const Icon = metric.icon;
            return (
              <div key={metric.label} className="flex items-center gap-3 px-4 py-3.5 sm:px-5">
                <Icon className="h-4 w-4 shrink-0" style={{ color: metric.color }} />
                <div>
                  <p className="text-sm font-semibold" style={{ color: "var(--ink-strong)" }}>
                    {metric.value}
                  </p>
                  <p className="text-[10px] uppercase tracking-wider" style={{ color: "var(--ink-muted)" }}>
                    {metric.label}
                  </p>
                </div>
              </div>
            );
          })}
        </div>
      </section>

      {preview.note && (
        <div className="panel-muted flex items-start gap-3 p-4">
          <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0" style={{ color: "var(--ok)" }} />
          <div>
            <p className="text-xs font-semibold" style={{ color: "var(--ink-strong)" }}>
              Simulation note
            </p>
            <p className="mt-1 text-xs leading-5" style={{ color: "var(--ink-soft)" }}>
              {preview.note}
            </p>
          </div>
        </div>
      )}

      <section className="panel p-4 sm:p-5">
        <div className="mb-4 flex items-center justify-between gap-3">
          <div>
            <p className="label-eyebrow mb-1">Execution trace</p>
            <h3 className="text-sm font-semibold" style={{ color: "var(--ink-strong)" }}>
              Stage timeline
            </h3>
          </div>
          <span className="pill pill-muted">{summary.events.length} stages</span>
        </div>

        {summary.events.length === 0 ? (
          <div className="panel-muted flex items-center gap-3 p-4 text-xs" style={{ color: "var(--ink-soft)" }}>
            <CircleDashed className="h-4 w-4" />
            This run did not emit any terminal stage events.
          </div>
        ) : (
          <ol className="space-y-2">
            {summary.events.map((event, index) => (
              <li key={`${event.stage}-${event.phase}-${index}`} className="flex gap-3">
                <div className="flex w-7 shrink-0 flex-col items-center">
                  <span
                    className="flex h-7 w-7 items-center justify-center rounded-full border"
                    style={{
                      background: "var(--surface)",
                      borderColor: statusColor(event.phase),
                      color: statusColor(event.phase),
                    }}
                  >
                    {event.phase === "completed" ? (
                      <CheckCircle2 className="h-3.5 w-3.5" />
                    ) : event.phase === "failed" ? (
                      <AlertCircle className="h-3.5 w-3.5" />
                    ) : event.phase === "skipped" ? (
                      <SkipForward className="h-3.5 w-3.5" />
                    ) : (
                      <CircleDashed className="h-3.5 w-3.5" />
                    )}
                  </span>
                  {index < summary.events.length - 1 && (
                    <span className="mt-1 min-h-4 w-px flex-1" style={{ background: "var(--border-subtle)" }} />
                  )}
                </div>
                <div className="panel-muted min-w-0 flex-1 px-3 py-2.5">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <span className="font-mono text-xs font-semibold" style={{ color: "var(--ink-strong)" }}>
                      {event.stage}
                    </span>
                    <span className="flex items-center gap-2">
                      <span className={statusPillClass(event.phase)}>{event.phase}</span>
                      {event.duration_ms != null && (
                        <span className="font-mono text-[10px]" style={{ color: "var(--ink-muted)" }}>
                          {formatDuration(event.duration_ms)}
                        </span>
                      )}
                    </span>
                  </div>
                  {event.message && (
                    <p className="mt-2 text-xs leading-5" style={{ color: "var(--ink-soft)" }}>
                      {event.message}
                    </p>
                  )}
                  {event.error && (
                    <p className="mt-2 flex items-start gap-2 text-xs leading-5" style={{ color: "var(--error-ink)" }}>
                      <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                      {event.error}
                    </p>
                  )}
                </div>
              </li>
            ))}
          </ol>
        )}
      </section>

      <section className="panel overflow-hidden">
        <div
          className="flex flex-wrap items-center justify-between gap-3 border-b px-4 py-3 sm:px-5"
          style={{ borderColor: "var(--border-subtle)" }}
        >
          <div>
            <p className="label-eyebrow mb-1">Artifacts</p>
            <h3 className="flex items-center gap-2 text-sm font-semibold" style={{ color: "var(--ink-strong)" }}>
              <Braces className="h-4 w-4" style={{ color: "var(--accent-soft-ink)" }} />
              Stage outputs
            </h3>
          </div>
          <span className="pill pill-muted">{outputCount} {outputCount === 1 ? "output" : "outputs"}</span>
        </div>
        <pre
          className="max-h-[34rem] overflow-auto p-4 font-mono text-xs leading-5 sm:p-5"
          style={{ background: "var(--surface-raised)", color: "var(--ink)" }}
          tabIndex={0}
          aria-label="Dry-run stage outputs"
        >
          {JSON.stringify(preview.outputs, null, 2)}
        </pre>
      </section>
    </div>
  );
}
