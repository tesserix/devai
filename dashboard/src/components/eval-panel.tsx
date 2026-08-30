"use client";

import { useCallback, useEffect, useState } from "react";
import { CheckCircle2, ListChecks, Play, Plus, Trash2, XCircle } from "lucide-react";
import { formatEvalCost, type EvalCost } from "@/lib/eval-cost";
import { lifecycleMutationHeaders } from "@/lib/api";

/**
 * Checks — the saved inputs and expectations an agent has to keep satisfying.
 *
 * A chat turn says what the agent did once; a suite says whether it still
 * behaves after the prompt changed. Cases live on the agent definition, so they
 * are published and versioned with it rather than kept in a side channel.
 */

export type EvalCase = {
  name: string;
  input: string;
  expect: {
    contains?: string[];
    not_contains?: string[];
    max_total_tokens?: number | null;
  };
};

type CaseResult = {
  name: string;
  passed: boolean;
  failures: string[];
  final_text: string;
  totals: { total_tokens?: number; latency_ms?: number };
};

type EvalRun = {
  id: string;
  created_at: string;
  status?: string;
  results: CaseResult[];
  summary: {
    cases: number;
    passed: number;
    failed: number;
    pass_rate: number;
    total_tokens: number;
    duration_ms: number;
  } & EvalCost;
};

const field =
  "w-full px-2.5 py-1 rounded-md border border-[var(--surface-border)] bg-[var(--surface-2)] text-xs text-[var(--ink-100)] placeholder-[var(--ink-500)] focus:outline-none focus:border-indigo-500/50";

function list(value: string): string[] {
  return value.split(",").map((v) => v.trim()).filter(Boolean);
}

export function EvalPanel({
  sandboxId,
  cases,
  onCasesChange,
}: {
  sandboxId: string;
  cases: EvalCase[];
  onCasesChange?: (cases: EvalCase[]) => void;
}) {
  const [runs, setRuns] = useState<EvalRun[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const editable = Boolean(onCasesChange);

  const loadRuns = useCallback(async () => {
    const res = await fetch(`/api/sandboxes/${encodeURIComponent(sandboxId)}/evals`, {
      credentials: "include",
    });
    if (res.ok) setRuns((await res.json()) as EvalRun[]);
  }, [sandboxId]);

  useEffect(() => {
    loadRuns().catch(() => setRuns([]));
  }, [loadRuns]);

  function update(index: number, patch: Partial<EvalCase>) {
    onCasesChange?.(cases.map((c, i) => (i === index ? { ...c, ...patch } : c)));
  }

  async function run() {
    setBusy(true);
    setError(null);
    try {
      const res = await fetch(`/api/sandboxes/${encodeURIComponent(sandboxId)}/evals`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json", ...lifecycleMutationHeaders() },
        body: JSON.stringify({ cases }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(typeof body.detail === "string" ? body.detail : `HTTP ${res.status}`);
      // The suite finishes in the background; poll the durable record until it
      // reaches a terminal status so long runs survive proxy timeouts.
      let run = body as EvalRun;
      const runUrl = `/api/sandboxes/${encodeURIComponent(sandboxId)}/evals/${encodeURIComponent(run.id)}`;
      const deadline = Date.now() + 15 * 60_000;
      while (run.status === "running" && Date.now() < deadline) {
        await new Promise((resolve) => setTimeout(resolve, 3000));
        const poll = await fetch(runUrl, { credentials: "include" });
        if (poll.ok) run = (await poll.json()) as EvalRun;
      }
      if (run.status === "running") throw new Error("evaluation is still running; refresh to see the result");
      if (run.status === "failed") throw new Error("evaluation failed to complete");
      setRuns((prev) => [run, ...prev]);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="panel p-0 overflow-hidden">
      <div className="px-4 py-2 border-b border-[var(--surface-border)] flex items-center justify-between gap-2">
        <span className="label-eyebrow flex items-center gap-1.5">
          <ListChecks className="w-3.5 h-3.5" /> Checks
        </span>
        <div className="flex items-center gap-2">
          {editable && (
            <button
              type="button"
              onClick={() => onCasesChange?.([...cases, { name: "", input: "", expect: {} }])}
              className="btn-secondary !py-1 !px-2 !text-[11px]"
            >
              <Plus className="w-3 h-3" /> Case
            </button>
          )}
          <button
            type="button"
            onClick={run}
            disabled={busy || cases.length === 0}
            className="btn-primary !py-1 !px-2 !text-[11px] disabled:opacity-50"
          >
            <Play className="w-3 h-3" /> {busy ? "Running…" : `Run ${cases.length || ""} checks`}
          </button>
        </div>
      </div>

      {cases.length === 0 ? (
        <p className="px-4 py-3 text-xs text-[var(--ink-500)]">
          No checks yet. A check is an input plus what the answer must contain — the difference between
          &ldquo;it worked when I tried it&rdquo; and a definition you can change safely.
        </p>
      ) : (
        <ul className="divide-y divide-[var(--surface-border)]">
          {cases.map((c, i) => (
            <li key={i} className="px-4 py-2.5 space-y-1.5">
              {editable ? (
                <>
                  <div className="flex gap-2">
                    <input
                      value={c.name}
                      onChange={(e) => update(i, { name: e.target.value })}
                      placeholder="What this proves, e.g. mentions the version"
                      className={field}
                    />
                    <button
                      type="button"
                      onClick={() => onCasesChange?.(cases.filter((_, j) => j !== i))}
                      className="shrink-0 px-2 rounded-md text-[var(--ink-500)] hover:text-red-300"
                      aria-label="Remove case"
                    >
                      <Trash2 className="w-3.5 h-3.5" />
                    </button>
                  </div>
                  <input
                    value={c.input}
                    onChange={(e) => update(i, { input: e.target.value })}
                    placeholder="Input sent to the agent"
                    className={`${field} font-mono`}
                  />
                  <div className="grid grid-cols-3 gap-2">
                    <input
                      value={(c.expect.contains ?? []).join(", ")}
                      onChange={(e) => update(i, { expect: { ...c.expect, contains: list(e.target.value) } })}
                      placeholder="must contain: v2.1, notes"
                      className={field}
                    />
                    <input
                      value={(c.expect.not_contains ?? []).join(", ")}
                      onChange={(e) =>
                        update(i, { expect: { ...c.expect, not_contains: list(e.target.value) } })
                      }
                      placeholder="must not contain: sorry"
                      className={field}
                    />
                    <input
                      type="number"
                      min={1}
                      value={c.expect.max_total_tokens ?? ""}
                      onChange={(e) =>
                        update(i, {
                          expect: {
                            ...c.expect,
                            max_total_tokens: e.target.value ? Number(e.target.value) : null,
                          },
                        })
                      }
                      placeholder="token budget"
                      className={field}
                    />
                  </div>
                </>
              ) : (
                <>
                  <div className="text-xs text-[var(--ink-100)]">{c.name || "(unnamed)"}</div>
                  <div className="text-[11px] font-mono text-[var(--ink-500)] truncate">{c.input}</div>
                </>
              )}
            </li>
          ))}
        </ul>
      )}

      {error && (
        <div className="mx-4 my-3 rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-300 font-mono">
          {error}
        </div>
      )}

      {runs.length > 0 && (
        <div className="border-t border-[var(--surface-border)]">
          {runs.map((r) => (
            <article key={r.id} className="border-b border-[var(--surface-border)] last:border-0">
              <div className="px-4 py-2 flex flex-wrap items-center gap-3 text-xs">
                <span
                  className={`font-medium ${r.summary.failed === 0 ? "text-emerald-300" : "text-red-300"}`}
                >
                  {r.summary.passed}/{r.summary.cases} passed
                </span>
                <span className="text-[var(--ink-500)]">{r.summary.total_tokens} tokens</span>
                <span className="text-[var(--ink-500)]">{r.summary.duration_ms} ms</span>
                <span className="text-[var(--ink-500)]">{formatEvalCost(r.summary)}</span>
                <span className="ml-auto font-mono text-[var(--ink-500)]">{r.id}</span>
              </div>
              <ul className="pb-2">
                {r.results.map((res, i) => (
                  <li key={`${r.id}-${i}`} className="px-4 py-1 flex items-start gap-2 text-xs">
                    {res.passed ? (
                      <CheckCircle2 className="w-3.5 h-3.5 mt-0.5 shrink-0 text-emerald-400" />
                    ) : (
                      <XCircle className="w-3.5 h-3.5 mt-0.5 shrink-0 text-red-400" />
                    )}
                    <div className="min-w-0">
                      <div className="text-[var(--ink-100)]">{res.name}</div>
                      {res.failures.length > 0 && (
                        <div className="text-[11px] text-red-300 font-mono">{res.failures.join(" · ")}</div>
                      )}
                      {res.final_text && (
                        <div className="text-[11px] text-[var(--ink-500)] truncate">{res.final_text}</div>
                      )}
                    </div>
                  </li>
                ))}
              </ul>
            </article>
          ))}
        </div>
      )}
    </section>
  );
}
