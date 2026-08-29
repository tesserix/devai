"use client";

import { useState } from "react";
import { CheckCircle2, Columns2, Play, XCircle } from "lucide-react";
import type { EvalCase } from "@/components/eval-panel";
import { lifecycleMutationHeaders } from "@/lib/api";

/**
 * The same checks, the same definition, two models.
 *
 * Choosing a model by reading two separate runs a day apart compares the noise
 * as much as the models. Both sides here run the identical suite against the
 * identical draft, so the only variable left is the model.
 */

type Side = {
  label: string;
  passed: number;
  cases: number;
  total_tokens: number;
  duration_ms: number;
  results: { name: string; passed: boolean; failures: string[] }[];
};

type EvalRun = {
  results: { name: string; passed: boolean; failures: string[] }[];
  summary: { cases: number; passed: number; total_tokens: number; duration_ms: number };
};

const field =
  "px-2.5 py-1 rounded-md border border-[var(--surface-border)] bg-[var(--surface-2)] text-xs text-[var(--ink-100)] placeholder-[var(--ink-500)] focus:outline-none focus:border-indigo-500/50";

async function post<T>(url: string, body: unknown): Promise<T> {
  const res = await fetch(url, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json", ...lifecycleMutationHeaders() },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : `HTTP ${res.status}`);
  return data as T;
}

export function ModelCompare({
  sandboxId,
  agentName,
  draft,
  cases,
  provider,
  model,
}: {
  sandboxId: string;
  agentName: string;
  draft: unknown;
  cases: EvalCase[];
  provider: string;
  model: string;
}) {
  const [rival, setRival] = useState("");
  const [sides, setSides] = useState<Side[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function compare() {
    setBusy(true);
    setError(null);
    try {
      const created = await post<{ id: string }>("/api/sandboxes", {
        agent: { name: agentName, version: "draft" },
        model: { provider, model: rival },
        draft,
        tools: { default_mode: "mock" },
        ttl_seconds: 3600,
      });
      const [a, b] = await Promise.all([
        post<EvalRun>(`/api/sandboxes/${encodeURIComponent(sandboxId)}/evals`, { cases }),
        post<EvalRun>(`/api/sandboxes/${encodeURIComponent(created.id)}/evals`, { cases }),
      ]);
      const side = (label: string, run: EvalRun): Side => ({ label, ...run.summary, results: run.results });
      setSides([side(model, a), side(rival, b)]);
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
          <Columns2 className="w-3.5 h-3.5" /> Compare models
        </span>
        <div className="flex items-center gap-2">
          <input
            value={rival}
            onChange={(e) => setRival(e.target.value)}
            placeholder={`vs. another ${provider} model`}
            className={`${field} font-mono w-64`}
            aria-label="Model to compare against"
          />
          <button
            type="button"
            onClick={compare}
            disabled={busy || !rival.trim() || rival === model || cases.length === 0}
            className="btn-primary !py-1 !px-2 !text-[11px] disabled:opacity-50"
          >
            <Play className="w-3 h-3" /> {busy ? "Running both…" : "Run both"}
          </button>
        </div>
      </div>

      {error && (
        <div className="mx-4 my-3 rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-300 font-mono">
          {error}
        </div>
      )}

      {!sides ? (
        <p className="px-4 py-3 text-xs text-[var(--ink-500)]">
          {cases.length === 0
            ? "Add a check first — a comparison with nothing to measure is just two chat replies."
            : `Runs all ${cases.length} checks against ${model} and a second model, and shows where they differ.`}
        </p>
      ) : (
        <div className="grid grid-cols-2 divide-x divide-[var(--surface-border)]">
          {sides.map((s) => (
            <div key={s.label} className="p-4 space-y-2">
              <div className="font-mono text-xs text-[var(--ink-100)]">{s.label}</div>
              <div className="flex gap-3 text-xs">
                <span className={s.passed === s.cases ? "text-emerald-300" : "text-red-300"}>
                  {s.passed}/{s.cases} passed
                </span>
                <span className="text-[var(--ink-500)]">{s.total_tokens} tokens</span>
                <span className="text-[var(--ink-500)]">{s.duration_ms} ms</span>
              </div>
              <ul className="space-y-1">
                {s.results.map((r, i) => (
                  <li key={`${s.label}-${i}`} className="flex items-start gap-2 text-xs">
                    {r.passed ? (
                      <CheckCircle2 className="w-3.5 h-3.5 mt-0.5 shrink-0 text-emerald-400" />
                    ) : (
                      <XCircle className="w-3.5 h-3.5 mt-0.5 shrink-0 text-red-400" />
                    )}
                    <div className="min-w-0">
                      <div className="text-[var(--ink-100)]">{r.name || "(unnamed)"}</div>
                      {r.failures.length > 0 && (
                        <div className="text-[11px] text-red-300 font-mono">{r.failures.join(" · ")}</div>
                      )}
                    </div>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
