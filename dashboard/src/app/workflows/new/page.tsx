"use client";

import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { Check, GitBranch, Loader2, Play, Plus, Trash2 } from "lucide-react";

import { api, type RegistryItem } from "@/lib/api";
import {
  blueprintFromGraph,
  STAGE_KEYS,
  STAGE_TYPES,
  type BuilderStage,
} from "@/lib/blueprintFromGraph";

let _seq = 0;
const newId = () => `s${++_seq}`;

/**
 * Visual blueprint builder — compose a DAG of stages (each optionally running a
 * composed agent via run_specialization), wire dependencies, preview the graph,
 * and publish. Publishing POSTs the generated YAML to /api/authoring/blueprints,
 * which validates it through the same loader as on-disk blueprints and pushes a
 * Blueprint envelope to the registry.
 */
export default function NewWorkflowPage() {
  const router = useRouter();

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [stages, setStages] = useState<BuilderStage[]>([
    { id: newId(), name: "start", type: "context", stageKey: "context_hydration", dependsOn: [] },
  ]);
  const [agents, setAgents] = useState<RegistryItem[]>([]);

  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [issues, setIssues] = useState<string[]>([]);
  const [done, setDone] = useState<string | null>(null);

  useEffect(() => {
    api.listRegistryAgents().then(setAgents).catch(() => setAgents([]));
  }, []);

  function patch(id: string, p: Partial<BuilderStage>) {
    setStages((ss) => ss.map((s) => (s.id === id ? { ...s, ...p } : s)));
  }
  function addStage() {
    setStages((ss) => [
      ...ss,
      { id: newId(), name: `stage-${ss.length + 1}`, type: "agentic", stageKey: "run_specialization", dependsOn: [], agent: "" },
    ]);
  }
  function removeStage(id: string) {
    setStages((ss) => {
      const gone = ss.find((s) => s.id === id);
      return ss
        .filter((s) => s.id !== id)
        .map((s) => (gone ? { ...s, dependsOn: s.dependsOn.filter((d) => d !== gone.name) } : s));
    });
  }
  function toggleDep(id: string, depName: string) {
    setStages((ss) =>
      ss.map((s) => {
        if (s.id !== id) return s;
        const has = s.dependsOn.includes(depName);
        return { ...s, dependsOn: has ? s.dependsOn.filter((d) => d !== depName) : [...s.dependsOn, depName] };
      }),
    );
  }

  const preview = useMemo(() => layout(stages), [stages]);

  async function publish() {
    setError(null);
    setIssues([]);
    setDone(null);
    const { yaml, errors } = blueprintFromGraph(name, description, stages);
    if (errors.length) {
      setIssues(errors);
      return;
    }
    setSaving(true);
    try {
      const res = await api.createBlueprint(yaml);
      setDone(res.name);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }

  // Dispatch the just-published blueprint and jump to its live run.
  async function runNow() {
    if (!done) return;
    setError(null);
    setSaving(true);
    try {
      const res = await api.dispatchCompose({
        intent: description.trim() || `Run blueprint ${done}`,
        repo: "",
        blueprint: done,
        label: "blueprint-builder",
      });
      router.push(`/runs/${res.task_id}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setSaving(false);
    }
  }

  return (
    <div className="p-7 space-y-6 max-w-5xl">
      <header>
        <div className="label-eyebrow">Authoring</div>
        <h1 className="font-serif text-2xl font-medium text-[var(--ink-50)] mt-1 flex items-center gap-2">
          <GitBranch className="w-5 h-5 text-indigo-400" /> Build Blueprint
        </h1>
        <p className="text-sm text-[var(--ink-300)] mt-1">
          Compose a DAG of stages, wire their dependencies, and publish. Agentic stages run a composed
          agent; publishing validates the blueprint and registers it.
        </p>
      </header>

      {error && <Banner kind="error">{error}</Banner>}
      {issues.length > 0 && (
        <Banner kind="error">
          <ul className="list-disc list-inside space-y-0.5">
            {issues.map((i) => (
              <li key={i}>{i}</li>
            ))}
          </ul>
        </Banner>
      )}
      {done && (
        <Banner kind="ok">
          <div className="flex items-center justify-between gap-3 flex-wrap">
            <span className="flex items-center gap-2">
              <Check className="w-4 h-4" /> Published blueprint <span className="font-mono">{done}</span>. It's registered and runnable.
            </span>
            <span className="flex items-center gap-2">
              <button type="button" onClick={runNow} disabled={saving} className="inline-flex items-center gap-1.5 px-3 py-1 rounded-md bg-emerald-600 hover:bg-emerald-500 disabled:opacity-50 text-white text-xs font-medium">
                {saving ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Play className="w-3.5 h-3.5" />} Run now
              </button>
              <button type="button" onClick={() => router.push("/workflows")} className="px-3 py-1 rounded-md border border-emerald-500/40 text-xs text-emerald-200 hover:border-emerald-400">
                Done
              </button>
            </span>
          </div>
        </Banner>
      )}

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <Field label="Name (kebab-case)">
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="my-flow" className={inputCls} />
        </Field>
        <Field label="Description">
          <input value={description} onChange={(e) => setDescription(e.target.value)} placeholder="What this blueprint does" className={inputCls} />
        </Field>
      </div>

      {/* DAG preview */}
      <div className="panel p-3">
        <div className="label-eyebrow mb-2">Graph preview</div>
        <DagPreview preview={preview} />
      </div>

      {/* Stage editor */}
      <div className="space-y-3">
        <div className="flex items-center justify-between">
          <label className="label-eyebrow">Stages ({stages.length})</label>
          <button type="button" onClick={addStage} className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md border border-[var(--surface-border)] text-sm text-[var(--ink-200)] hover:border-[var(--surface-border-strong)]">
            <Plus className="w-4 h-4" /> Add stage
          </button>
        </div>

        {stages.map((s) => (
          <div key={s.id} className="panel p-3 space-y-3">
            <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
              <Field label="Name">
                <input value={s.name} onChange={(e) => patch(s.id, { name: e.target.value })} className={inputCls} />
              </Field>
              <Field label="Type">
                <select value={s.type} onChange={(e) => patch(s.id, { type: e.target.value })} className={inputCls}>
                  {STAGE_TYPES.map((t) => (
                    <option key={t} value={t}>{t}</option>
                  ))}
                </select>
              </Field>
              <Field label="Stage">
                <select value={s.stageKey} onChange={(e) => patch(s.id, { stageKey: e.target.value })} className={inputCls}>
                  {STAGE_KEYS.map((k) => (
                    <option key={k} value={k}>{k}</option>
                  ))}
                </select>
              </Field>
            </div>

            {s.stageKey === "run_specialization" && (
              <Field label="Agent (specialization)">
                <input
                  list={`agents-${s.id}`}
                  value={s.agent ?? ""}
                  onChange={(e) => patch(s.id, { agent: e.target.value })}
                  placeholder="composed agent name"
                  className={inputCls}
                />
                <datalist id={`agents-${s.id}`}>
                  {agents.map((a) => (
                    <option key={a.name} value={a.name} />
                  ))}
                </datalist>
              </Field>
            )}

            <div>
              <span className="label-eyebrow">Depends on</span>
              <div className="flex flex-wrap gap-1.5 mt-1.5">
                {stages.filter((o) => o.id !== s.id).length === 0 ? (
                  <span className="text-xs text-[var(--ink-500)]">no other stages yet</span>
                ) : (
                  stages
                    .filter((o) => o.id !== s.id)
                    .map((o) => {
                      const on = s.dependsOn.includes(o.name);
                      return (
                        <button
                          key={o.id}
                          type="button"
                          onClick={() => toggleDep(s.id, o.name)}
                          className={
                            "px-2 py-1 rounded-md border text-xs font-mono transition-colors " +
                            (on ? "border-indigo-500/60 bg-indigo-500/10 text-[var(--ink-100)]" : "border-[var(--surface-border)] text-[var(--ink-400)] hover:border-[var(--surface-border-strong)]")
                          }
                        >
                          {o.name}
                        </button>
                      );
                    })
                )}
              </div>
            </div>

            <div className="flex justify-end">
              <button type="button" onClick={() => removeStage(s.id)} className="inline-flex items-center gap-1.5 text-xs text-red-400 hover:text-red-300">
                <Trash2 className="w-3.5 h-3.5" /> Remove
              </button>
            </div>
          </div>
        ))}
      </div>

      <div className="flex items-center gap-3">
        <button type="button" onClick={publish} disabled={saving} className="inline-flex items-center gap-2 px-4 py-2 rounded-md bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 text-white text-sm font-medium">
          {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <GitBranch className="w-4 h-4" />}
          Publish blueprint
        </button>
        <button type="button" onClick={() => router.push("/workflows")} className="px-4 py-2 rounded-md border border-[var(--surface-border)] text-sm text-[var(--ink-200)] hover:border-[var(--surface-border-strong)]">
          Cancel
        </button>
      </div>
    </div>
  );
}

// ── DAG layout + preview ──────────────────────────────────────────────

interface Placed {
  name: string;
  col: number;
  row: number;
}
interface Preview {
  placed: Placed[];
  edges: Array<{ from: string; to: string }>;
  cols: number;
  rows: number;
}

// Longest-path layering: a stage sits one column right of its deepest dep.
function layout(stages: BuilderStage[]): Preview {
  const byName = new Map(stages.map((s) => [s.name, s]));
  const depth = new Map<string, number>();
  function d(name: string, seen: Set<string>): number {
    if (depth.has(name)) return depth.get(name)!;
    if (seen.has(name)) return 0; // cycle guard
    seen.add(name);
    const s = byName.get(name);
    const deps = s?.dependsOn.filter((x) => byName.has(x)) ?? [];
    const val = deps.length ? 1 + Math.max(...deps.map((x) => d(x, seen))) : 0;
    depth.set(name, val);
    return val;
  }
  stages.forEach((s) => d(s.name, new Set()));

  const rowByCol = new Map<number, number>();
  const placed: Placed[] = stages.map((s) => {
    const col = depth.get(s.name) ?? 0;
    const row = rowByCol.get(col) ?? 0;
    rowByCol.set(col, row + 1);
    return { name: s.name, col, row };
  });
  const edges = stages.flatMap((s) =>
    s.dependsOn.filter((x) => byName.has(x)).map((from) => ({ from, to: s.name })),
  );
  const cols = Math.max(1, ...placed.map((p) => p.col + 1));
  const rows = Math.max(1, ...Array.from(rowByCol.values()));
  return { placed, edges, cols, rows };
}

function DagPreview({ preview }: { preview: Preview }) {
  const { placed, edges, cols, rows } = preview;
  const CW = 150;
  const CH = 52;
  const NW = 120;
  const NH = 30;
  const width = cols * CW + 20;
  const height = rows * CH + 20;
  const pos = new Map(placed.map((p) => [p.name, { x: p.col * CW + 10, y: p.row * CH + 10 }]));
  if (placed.length === 0) return <div className="text-xs text-[var(--ink-500)] p-3">No stages.</div>;
  return (
    <div className="overflow-auto">
      <svg width={width} height={height} className="min-w-full">
        {edges.map((e, i) => {
          const a = pos.get(e.from);
          const b = pos.get(e.to);
          if (!a || !b) return null;
          return (
            <line
              key={i}
              x1={a.x + NW}
              y1={a.y + NH / 2}
              x2={b.x}
              y2={b.y + NH / 2}
              stroke="#7c83f0"
              strokeWidth={1.5}
              markerEnd="url(#arrow)"
            />
          );
        })}
        <defs>
          <marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto">
            <path d="M0,0 L7,3 L0,6 Z" fill="#7c83f0" />
          </marker>
        </defs>
        {placed.map((p) => {
          const xy = pos.get(p.name)!;
          return (
            <g key={p.name}>
              <rect x={xy.x} y={xy.y} width={NW} height={NH} rx={6} fill="rgba(99,102,241,0.13)" stroke="#6366f1" strokeWidth={1.2} />
              <text x={xy.x + NW / 2} y={xy.y + NH / 2 + 4} textAnchor="middle" fontSize={11} fontFamily="monospace" fill="var(--ink-100)">
                {p.name.length > 16 ? p.name.slice(0, 15) + "…" : p.name}
              </text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}

const inputCls =
  "w-full px-3 py-2 rounded-md border border-[var(--surface-border)] bg-[var(--surface-2)] text-sm text-[var(--ink-100)] placeholder-[var(--ink-500)] focus:outline-none focus:border-indigo-500/50";

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block space-y-1.5">
      <span className="label-eyebrow">{label}</span>
      {children}
    </label>
  );
}

function Banner({ kind, children }: { kind: "error" | "ok"; children: React.ReactNode }) {
  const cls =
    kind === "error"
      ? "border-red-500/30 bg-red-500/10 text-red-300"
      : "border-emerald-500/30 bg-emerald-500/10 text-emerald-300";
  return <div className={`rounded-md border px-3 py-2 text-sm ${cls}`}>{children}</div>;
}
