"use client";

import type React from "react";
import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { AlertTriangle, Check, GitBranch, Loader2, Play, Plus, Trash2, Users } from "lucide-react";

import { api, type RegistryItem } from "@/lib/api";
import { Breadcrumbs } from "@/components/breadcrumbs";
import { GuidanceInfo, GuidancePanel, HelpPopover } from "@/components/guidance";
import { Select } from "@/components/ui/select";
import {
  blueprintFromGraph,
  validateConditionKeys,
  SEED_CREWS,
  STAGE_KEYS,
  STAGE_TYPES,
  TASK_BOOL_KEYS,
  type BuilderStage,
} from "@/lib/blueprintFromGraph";

let _seq = 0;
const newId = () => `s${++_seq}`;

/**
 * Visual blueprint builder — compose a DAG of stages, wire dependencies, preview
 * the graph, and publish. Agentic stages run a composed agent (run_specialization
 * → config.specialization); crew stages run a lead+members crew (run_crew →
 * config.crew). Stages can carry an optional condition that skips them.
 *
 * Publishing POSTs the generated YAML to /api/authoring/blueprints, which
 * validates it through the same loader as on-disk blueprints. We validate the
 * same things up front (DASH-9) so the user sees problems inline instead of a
 * 400 from the backend: unknown agents and empty/unknown crews silently no-op at
 * runtime, and a typo'd condition key would make a gate run unconditionally.
 */
export default function NewWorkflowPage() {
  const router = useRouter();

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [stages, setStages] = useState<BuilderStage[]>([
    { id: newId(), name: "start", type: "context", stageKey: "context_hydration", dependsOn: [] },
  ]);
  const [agents, setAgents] = useState<RegistryItem[]>([]);
  const [crews, setCrews] = useState<string[]>([]);

  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [issues, setIssues] = useState<string[]>([]);
  const [done, setDone] = useState<string | null>(null);

  // Load the registered agents (block publish on unknown names) and resolvable
  // crews (seed crews + every team's DB crews) for the pickers.
  useEffect(() => {
    api.listRegistryAgents().then(setAgents).catch(() => setAgents([]));
  }, []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const names = new Set<string>(SEED_CREWS);
      try {
        const teams = await api.listTeams();
        const perTeam = await Promise.all(
          teams.map((t) => api.listCrews(t.id).catch(() => [])),
        );
        for (const list of perTeam) for (const c of list) if (c?.name) names.add(c.name);
      } catch {
        // Teams may be disabled — seed crews still let the picker work.
      }
      if (!cancelled) setCrews([...names].sort());
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const agentNames = useMemo(() => new Set(agents.map((a) => a.name)), [agents]);
  const crewNames = useMemo(() => new Set(crews), [crews]);

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
    const { yaml, errors } = blueprintFromGraph(name, description, stages, {
      knownAgents: agentNames,
      knownCrews: crewNames,
    });
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
    <div className="p-7 space-y-5 w-full">
      <Breadcrumbs
        items={[
          { label: "Fleet", href: "/" },
          { label: "Workflows", href: "/workflows" },
          { label: "Build blueprint" },
        ]}
      />

      <header>
        <div className="label-eyebrow">Authoring</div>
        <h1
          className="font-serif text-2xl font-medium mt-1 flex items-center gap-2"
          style={{ color: "var(--ink-strong)" }}
        >
          <GitBranch className="w-5 h-5" style={{ color: "var(--accent)" }} /> Build blueprint
          <GuidanceInfo id="builder" className="ml-0.5 align-middle" />
        </h1>
        <p className="text-sm mt-1 max-w-2xl" style={{ color: "var(--ink-soft)" }}>
          Compose a DAG of stages, wire their dependencies, and publish. Agentic stages run a single
          composed agent; crew stages run a lead + members. The DAG you draw is the DAG that runs —
          conditions can only skip a stage, never add one.
        </p>
      </header>

      <GuidancePanel id="builder" />

      {error && <Banner kind="error">{error}</Banner>}
      {issues.length > 0 && (
        <Banner kind="error">
          <div className="flex items-start gap-2">
            <AlertTriangle className="w-4 h-4 mt-0.5 shrink-0" />
            <ul className="space-y-0.5">
              {issues.map((i) => (
                <li key={i}>{i}</li>
              ))}
            </ul>
          </div>
        </Banner>
      )}
      {done && (
        <Banner kind="ok">
          <div className="flex items-center justify-between gap-3 flex-wrap">
            <span className="flex items-center gap-2">
              <Check className="w-4 h-4" /> Published blueprint{" "}
              <span className="font-mono">{done}</span>. It&apos;s registered and runnable.
            </span>
            <span className="flex items-center gap-2">
              <button
                type="button"
                onClick={runNow}
                disabled={saving}
                className="btn-primary !py-1 !text-xs"
              >
                {saving ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Play className="w-3.5 h-3.5" />} Run now
              </button>
              <button
                type="button"
                onClick={() => router.push("/workflows")}
                className="btn-secondary !py-1 !text-xs"
              >
                Done
              </button>
            </span>
          </div>
        </Banner>
      )}

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <Field label="Name (kebab-case)">
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="my-flow"
            className="field w-full"
          />
        </Field>
        <Field label="Description">
          <input
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="What this blueprint does"
            className="field w-full"
          />
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
          <button type="button" onClick={addStage} className="btn-secondary">
            <Plus className="w-4 h-4" /> Add stage
          </button>
        </div>

        {stages.map((s) => (
          <StageEditor
            key={s.id}
            stage={s}
            allStages={stages}
            agents={agents}
            agentNames={agentNames}
            crews={crews}
            crewNames={crewNames}
            onPatch={(p) => patch(s.id, p)}
            onToggleDep={(dep) => toggleDep(s.id, dep)}
            onRemove={() => removeStage(s.id)}
          />
        ))}
      </div>

      <div className="flex items-center gap-3">
        <button type="button" onClick={publish} disabled={saving} className="btn-primary">
          {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <GitBranch className="w-4 h-4" />}
          Publish blueprint
        </button>
        <button type="button" onClick={() => router.push("/workflows")} className="btn-secondary">
          Cancel
        </button>
      </div>
    </div>
  );
}

// ── Stage editor row ───────────────────────────────────────────────────────

function StageEditor({
  stage: s,
  allStages,
  agents,
  agentNames,
  crews,
  crewNames,
  onPatch,
  onToggleDep,
  onRemove,
}: {
  stage: BuilderStage;
  allStages: BuilderStage[];
  agents: RegistryItem[];
  agentNames: Set<string>;
  crews: string[];
  crewNames: Set<string>;
  onPatch: (p: Partial<BuilderStage>) => void;
  onToggleDep: (dep: string) => void;
  onRemove: () => void;
}) {
  const agentVal = (s.agent ?? "").trim();
  const crewVal = (s.crew ?? "").trim();
  const agentUnknown =
    s.stageKey === "run_specialization" &&
    agentVal !== "" &&
    agentNames.size > 0 &&
    !agentNames.has(agentVal);
  const crewUnknown =
    s.stageKey === "run_crew" && crewVal !== "" && crewNames.size > 0 && !crewNames.has(crewVal);
  const condUnknown = validateConditionKeys(s.condition);

  return (
    <div className="panel p-3 space-y-3">
      <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
        <Field label="Name">
          <input value={s.name} onChange={(e) => onPatch({ name: e.target.value })} className="field w-full" />
        </Field>
        <Field label="Type">
          <Select
            value={s.type}
            onChange={(v) => onPatch({ type: v })}
            options={STAGE_TYPES.map((t) => ({ value: t, label: t }))}
            ariaLabel="Stage type"
          />
        </Field>
        <Field label="Stage">
          <Select
            value={s.stageKey}
            onChange={(v) => onPatch({ stageKey: v })}
            options={STAGE_KEYS.map((k) => ({ value: k, label: k }))}
            mono
            ariaLabel="Stage"
          />
        </Field>
      </div>

      {s.stageKey === "run_specialization" && (
        <Field label="Agent (specialization)">
          <input
            list={`agents-${s.id}`}
            value={s.agent ?? ""}
            onChange={(e) => onPatch({ agent: e.target.value })}
            placeholder="composed agent name"
            className="field w-full"
            style={agentUnknown ? { borderColor: "var(--error)" } : undefined}
            aria-invalid={agentUnknown}
          />
          <datalist id={`agents-${s.id}`}>
            {agents.map((a) => (
              <option key={a.name} value={a.name} />
            ))}
          </datalist>
          {agentUnknown ? (
            <InlineHint kind="error">
              &ldquo;{agentVal}&rdquo; isn&apos;t a registered agent — an unknown agent silently no-ops at runtime.
            </InlineHint>
          ) : agentVal === "" ? (
            <InlineHint kind="muted">Required. Pick a registered agent for this stage to run.</InlineHint>
          ) : null}
        </Field>
      )}

      {s.stageKey === "run_crew" && (
        <Field
          label={
            <span className="inline-flex items-center gap-1.5">
              <Users className="w-3 h-3" style={{ color: "var(--ink-muted)" }} /> Crew
              <HelpPopover term="crew" />
            </span>
          }
        >
          <input
            list={`crews-${s.id}`}
            value={s.crew ?? ""}
            onChange={(e) => onPatch({ crew: e.target.value })}
            placeholder="backend_crew"
            className="field w-full"
            style={crewUnknown ? { borderColor: "var(--error)" } : undefined}
            aria-invalid={crewUnknown}
          />
          <datalist id={`crews-${s.id}`}>
            {crews.map((c) => (
              <option key={c} value={c} />
            ))}
          </datalist>
          {crewUnknown ? (
            <InlineHint kind="error">
              &ldquo;{crewVal}&rdquo; isn&apos;t a resolvable crew. Pick a seed or team crew that exists.
            </InlineHint>
          ) : crewVal === "" ? (
            <InlineHint kind="error">
              Required. An empty crew resolves to <code>no_crew</code> and the stage does nothing.
            </InlineHint>
          ) : null}
        </Field>
      )}

      <Field
        label={
          <span className="inline-flex items-center gap-1.5">
            Condition <span style={{ color: "var(--ink-muted)" }}>(optional skip guard)</span>
            <HelpPopover term="condition" />
          </span>
        }
      >
        <input
          value={s.condition ?? ""}
          onChange={(e) => onPatch({ condition: e.target.value })}
          placeholder="e.g. task.has_pr  ·  !task.has_sandbox  ·  output.review_decision_approved"
          className="field w-full font-mono text-[12.5px]"
          style={condUnknown.length ? { borderColor: "var(--error)" } : undefined}
          aria-invalid={condUnknown.length > 0}
        />
        {condUnknown.length > 0 ? (
          <InlineHint kind="error">
            Unknown key{condUnknown.length > 1 ? "s" : ""} {condUnknown.map((k) => `"${k}"`).join(", ")}.
            Use a <code>task.*</code> flag or an <code>output.</code> / <code>state.</code> /{" "}
            <code>agent_context.</code> lookup.
          </InlineHint>
        ) : (
          <InlineHint kind="muted">
            Leave blank to always run. Flags: {TASK_BOOL_KEYS.slice(0, 4).join(", ")}…
          </InlineHint>
        )}
      </Field>

      <div>
        <span className="label-eyebrow">Depends on</span>
        <div className="flex flex-wrap gap-1.5 mt-1.5">
          {allStages.filter((o) => o.id !== s.id).length === 0 ? (
            <span className="text-xs" style={{ color: "var(--ink-muted)" }}>
              no other stages yet
            </span>
          ) : (
            allStages
              .filter((o) => o.id !== s.id)
              .map((o) => {
                const on = s.dependsOn.includes(o.name);
                return (
                  <button
                    key={o.id}
                    type="button"
                    onClick={() => onToggleDep(o.name)}
                    className="px-2 py-1 rounded-md border text-xs font-mono transition-colors"
                    style={{
                      borderColor: on ? "var(--accent-soft-bd)" : "var(--border-subtle)",
                      background: on ? "var(--accent-soft-bg-2)" : "transparent",
                      color: on ? "var(--ink-strong)" : "var(--ink-muted)",
                    }}
                  >
                    {o.name}
                  </button>
                );
              })
          )}
        </div>
      </div>

      <div className="flex justify-end">
        <button
          type="button"
          onClick={onRemove}
          className="inline-flex items-center gap-1.5 text-xs"
          style={{ color: "var(--error-ink)" }}
        >
          <Trash2 className="w-3.5 h-3.5" /> Remove
        </button>
      </div>
    </div>
  );
}

function InlineHint({ kind, children }: { kind: "error" | "muted"; children: React.ReactNode }) {
  return (
    <p
      className="mt-1 text-[11.5px] leading-snug"
      style={{ color: kind === "error" ? "var(--error-ink)" : "var(--ink-muted)" }}
    >
      {children}
    </p>
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
  if (placed.length === 0)
    return (
      <div className="text-xs p-3" style={{ color: "var(--ink-muted)" }}>
        No stages.
      </div>
    );
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
              stroke="var(--accent)"
              strokeWidth={1.5}
              markerEnd="url(#arrow)"
            />
          );
        })}
        <defs>
          <marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto">
            <path d="M0,0 L7,3 L0,6 Z" fill="var(--accent)" />
          </marker>
        </defs>
        {placed.map((p) => {
          const xy = pos.get(p.name)!;
          return (
            <g key={p.name}>
              <rect
                x={xy.x}
                y={xy.y}
                width={NW}
                height={NH}
                rx={6}
                fill="var(--accent-soft-bg)"
                stroke="var(--accent)"
                strokeWidth={1.2}
              />
              <text
                x={xy.x + NW / 2}
                y={xy.y + NH / 2 + 4}
                textAnchor="middle"
                fontSize={11}
                fontFamily="monospace"
                fill="var(--ink-strong)"
              >
                {p.name.length > 16 ? p.name.slice(0, 15) + "…" : p.name}
              </text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}

function Field({ label, children }: { label: React.ReactNode; children: React.ReactNode }) {
  return (
    <label className="block space-y-1.5">
      <span className="label-eyebrow">{label}</span>
      {children}
    </label>
  );
}

function Banner({ kind, children }: { kind: "error" | "ok"; children: React.ReactNode }) {
  const style =
    kind === "error"
      ? { background: "var(--error-soft-bg)", color: "var(--error-ink)", border: "1px solid var(--error-soft-bd)" }
      : { background: "var(--ok-soft-bg)", color: "var(--ok-ink)", border: "1px solid var(--ok-soft-bd)" };
  return (
    <div className="rounded-md px-3 py-2 text-sm" style={style}>
      {children}
    </div>
  );
}
