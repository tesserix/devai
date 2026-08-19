"use client";

import { useEffect, useMemo, useState } from "react";
import { CheckCircle2, FlaskConical, Play, XCircle } from "lucide-react";

import { HelpPopover } from "@/components/guidance";
import { Select } from "@/components/ui/select";
import {
  api,
  type ArtifactVersionRef,
  type EvaluationDataset,
  type EvaluationRun,
  type EvaluationSuite,
  type SandboxRecord,
} from "@/lib/api";
import { formatEvalCost } from "@/lib/eval-cost";

function refKey(ref: ArtifactVersionRef): string {
  return `${ref.name}@${ref.version}`;
}

export function AgentEvaluationPanel({
  sandbox,
  datasets,
  suites,
  runs,
  onRun,
  onOpenTrace,
}: {
  sandbox: SandboxRecord;
  datasets: EvaluationDataset[];
  suites: EvaluationSuite[];
  runs: EvaluationRun[];
  onRun: (run: EvaluationRun) => void;
  onOpenTrace: (traceId: string) => void;
}) {
  const [sourceKind, setSourceKind] = useState<"dataset" | "suite">("dataset");
  const [sourceKey, setSourceKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const sources = sourceKind === "dataset" ? datasets : suites;

  useEffect(() => {
    if (!sources.some((source) => refKey(source) === sourceKey)) {
      setSourceKey(sources[0] ? refKey(sources[0]) : "");
    }
  }, [sourceKey, sources]);

  const latest = runs[0];
  const sourceOptions = useMemo(
    () =>
      sources.map((source) => ({
        value: refKey(source),
        label: `${source.name}@${source.version}`,
        description: source.description,
        badge: "case_count" in source ? `${source.case_count} cases` : `${source.scorers.length} scorers`,
      })),
    [sources],
  );

  async function runEvaluation() {
    const source = sources.find((candidate) => refKey(candidate) === sourceKey);
    if (!source) return;
    setBusy(true);
    setError(null);
    try {
      const ref = { name: source.name, version: source.version };
      const run = await api.runSandboxEvaluation(
        sandbox.id,
        sourceKind === "dataset" ? { dataset: ref } : { suite: ref },
      );
      onRun(run);
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
              <FlaskConical className="w-4 h-4 text-indigo-300" /> Run an evaluation
              <HelpPopover term="evaluation" />
            </h2>
            <p className="mt-1 text-xs text-[var(--ink-500)]">
              Run the pinned agent against an immutable dataset or its scored suite. Results survive sandbox deletion.
            </p>
          </div>
          <button
            type="button"
            onClick={runEvaluation}
            disabled={busy || !sourceKey || sandbox.status !== "ready"}
            className="btn-primary !py-1 !px-3 !text-xs disabled:opacity-50"
          >
            <Play className="w-3 h-3" /> {busy ? "Running…" : "Run evaluation"}
          </button>
        </div>
        <div className="mt-4 grid gap-3 sm:grid-cols-[160px_1fr]">
          <div>
            <label className="label-eyebrow" htmlFor="eval-source-kind">Source</label>
            <Select
              id="eval-source-kind"
              value={sourceKind}
              onChange={(value) => setSourceKind(value as "dataset" | "suite")}
              options={[
                { value: "dataset", label: "Dataset", description: "Direct deterministic expectations" },
                { value: "suite", label: "Eval suite", description: "Dataset plus scorers and thresholds" },
              ]}
              ariaLabel="Evaluation source type"
            />
          </div>
          <div>
            <label className="label-eyebrow" htmlFor="eval-source">Versioned source</label>
            <Select
              id="eval-source"
              value={sourceKey}
              onChange={setSourceKey}
              options={sourceOptions}
              searchable
              mono
              placeholder={sources.length === 0 ? `No ${sourceKind}s available` : "Choose a version"}
              ariaLabel="Evaluation source"
            />
          </div>
        </div>
        {error && <p className="mt-3 text-xs font-mono text-red-300">{error}</p>}
      </div>

      {latest && (
        <div className="panel p-4">
          <div className="flex flex-wrap items-center gap-4 text-xs">
            <span className={latest.summary.failed === 0 ? "text-emerald-300" : "text-red-300"}>
              {latest.summary.passed}/{latest.summary.cases} passed
            </span>
            <span className="text-[var(--ink-300)]">P95 {latest.summary.p95_latency_ms} ms</span>
            <span className="text-[var(--ink-300)]">{latest.summary.total_tokens} tokens</span>
            <span className="text-[var(--ink-300)]">{formatEvalCost(latest.summary)}</span>
            <span className="ml-auto font-mono text-[var(--ink-500)]">{latest.id}</span>
          </div>
          {latest.summary.dimensions && Object.keys(latest.summary.dimensions).length > 0 && (
            <dl className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-4">
              {Object.entries(latest.summary.dimensions).map(([name, metric]) => (
                <div key={name} className="rounded border border-[var(--surface-border)] p-2">
                  <dt className="label-eyebrow">{name.replaceAll("_", " ")}</dt>
                  <dd className="mt-1 font-mono text-sm text-[var(--ink-100)]">
                    {(metric.average * 100).toFixed(1)}%
                  </dd>
                </div>
              ))}
            </dl>
          )}
          <ul className="mt-3 divide-y divide-[var(--surface-border)]">
            {latest.results.map((result) => (
              <li key={result.name} className="flex items-start gap-2 py-2 text-xs">
                {result.passed ? (
                  <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 shrink-0 text-emerald-400" />
                ) : (
                  <XCircle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-red-400" />
                )}
                <div className="min-w-0 flex-1">
                  <div className="text-[var(--ink-100)]">{result.name}</div>
                  {result.failures.length > 0 && (
                    <div className="font-mono text-[11px] text-red-300">{result.failures.join(" · ")}</div>
                  )}
                </div>
                {result.invocation_id && (
                  <button
                    type="button"
                    onClick={() => onOpenTrace(result.invocation_id)}
                    className="text-indigo-300 hover:underline"
                  >
                    Open trace
                  </button>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}

      {runs.length === 0 && (
        <p className="text-sm text-[var(--ink-500)]">No evaluation runs for this sandbox yet.</p>
      )}
    </section>
  );
}
