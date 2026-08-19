"use client";

import { useEffect, useMemo, useState } from "react";
import { GitCompareArrows } from "lucide-react";

import { HelpPopover } from "@/components/guidance";
import { Select } from "@/components/ui/select";
import { api, type EvaluationComparison, type EvaluationRun } from "@/lib/api";
import { comparisonDeltaTone } from "@/lib/agent-workbench";

function runLabel(run: EvaluationRun): string {
  const version = run.configuration?.agent?.version ?? "unknown";
  const dataset = run.dataset ? `${run.dataset.name}@${run.dataset.version}` : "inline cases";
  return `${version} · ${dataset} · ${(run.summary.pass_rate * 100).toFixed(1)}%`;
}

function traceId(url?: string | null): string {
  const value = url?.split("/").filter(Boolean).at(-1) ?? "";
  return decodeURIComponent(value);
}

export function AgentComparisonPanel({
  runs,
  onOpenTrace,
}: {
  runs: EvaluationRun[];
  onOpenTrace: (traceId: string) => void;
}) {
  const [baselineId, setBaselineId] = useState("");
  const [candidateId, setCandidateId] = useState("");
  const [comparison, setComparison] = useState<EvaluationComparison | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!runs.some((run) => run.id === candidateId)) setCandidateId(runs[0]?.id ?? "");
    if (!runs.some((run) => run.id === baselineId)) setBaselineId(runs[1]?.id ?? "");
  }, [baselineId, candidateId, runs]);

  const options = useMemo(
    () => runs.map((run) => ({ value: run.id, label: runLabel(run), description: run.id })),
    [runs],
  );

  async function compare() {
    setBusy(true);
    setError(null);
    try {
      setComparison(await api.createComparison({ baseline_run_id: baselineId, candidate_run_id: candidateId }));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="space-y-4">
      <div className="panel p-4">
        <div className="flex items-start justify-between gap-3">
          <div>
            <h2 className="text-sm font-medium text-[var(--ink-50)] flex items-center gap-1.5">
              <GitCompareArrows className="w-4 h-4 text-indigo-300" /> Compare versions
              <HelpPopover term="evaluation-comparison" />
            </h2>
            <p className="mt-1 text-xs text-[var(--ink-500)]">
              Choose two durable runs over the same dataset. DevAI calculates deltas and names every regression.
            </p>
          </div>
          <button
            type="button"
            onClick={compare}
            disabled={busy || !baselineId || !candidateId || baselineId === candidateId}
            className="btn-primary !py-1 !px-3 !text-xs disabled:opacity-50"
          >
            {busy ? "Comparing…" : "Compare"}
          </button>
        </div>
        <div className="mt-4 grid gap-3 md:grid-cols-2">
          <div>
            <label className="label-eyebrow" htmlFor="comparison-baseline">Production / baseline</label>
            <Select
              id="comparison-baseline"
              value={baselineId}
              onChange={setBaselineId}
              options={options}
              mono
              searchable
              placeholder="Choose a baseline run"
              ariaLabel="Baseline evaluation run"
            />
          </div>
          <div>
            <label className="label-eyebrow" htmlFor="comparison-candidate">Candidate</label>
            <Select
              id="comparison-candidate"
              value={candidateId}
              onChange={setCandidateId}
              options={options}
              mono
              searchable
              placeholder="Choose a candidate run"
              ariaLabel="Candidate evaluation run"
            />
          </div>
        </div>
        {error && <p className="mt-3 text-xs font-mono text-red-300">{error}</p>}
      </div>

      {comparison && (
        <div className="panel p-4 space-y-4">
          <div>
            <div className="label-eyebrow">Trade-off summary</div>
            <p className="mt-1 text-sm text-[var(--ink-100)]">{comparison.summary}</p>
            <p className="mt-1 text-xs text-[var(--ink-500)]">{comparison.caveat}</p>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead className="text-[var(--ink-500)]">
                <tr><th className="py-1">Metric</th><th>Baseline</th><th>Candidate</th><th>Delta</th></tr>
              </thead>
              <tbody className="divide-y divide-[var(--surface-border)]">
                {Object.entries(comparison.metrics).map(([name, metric]) => (
                  <tr key={name}>
                    <td className="py-2 text-[var(--ink-100)]">{name.replaceAll("_", " ")}</td>
                    <td className="font-mono text-[var(--ink-300)]">{metric.baseline.toFixed(4)}</td>
                    <td className="font-mono text-[var(--ink-300)]">{metric.candidate.toFixed(4)}</td>
                    <td className={`font-mono ${
                      comparisonDeltaTone(name, metric.delta) === "improved"
                        ? "text-emerald-300"
                        : comparisonDeltaTone(name, metric.delta) === "regressed"
                          ? "text-red-300"
                          : "text-[var(--ink-500)]"
                    }`}>
                      {metric.delta > 0 ? "+" : ""}{metric.delta.toFixed(4)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div>
            <div className="label-eyebrow">Regressions ({comparison.regressions.length})</div>
            {comparison.regressions.length === 0 ? (
              <p className="mt-2 text-xs text-emerald-300">No formerly passing case regressed.</p>
            ) : (
              <ul className="mt-2 divide-y divide-[var(--surface-border)]">
                {comparison.regressions.map((regression) => {
                  const candidateTrace = traceId(regression.candidate_trace_url);
                  return (
                    <li key={regression.case_id} className="flex items-center gap-3 py-2 text-xs">
                      <span className="text-red-300">{regression.case_id}</span>
                      <span className="text-[var(--ink-500)]">pass → fail</span>
                      {candidateTrace && (
                        <button
                          type="button"
                          onClick={() => onOpenTrace(candidateTrace)}
                          className="ml-auto text-indigo-300 hover:underline"
                        >
                          Open candidate trace
                        </button>
                      )}
                    </li>
                  );
                })}
              </ul>
            )}
          </div>
        </div>
      )}

      {runs.length < 2 && (
        <p className="text-sm text-[var(--ink-500)]">
          Run at least two evaluations for this agent before comparing versions.
        </p>
      )}
    </section>
  );
}
