"use client";

/**
 * BlueprintPicker — choose which blueprint a run dispatches (DASH-6).
 *
 * The main "New task" CTA used to send no blueprint, so the backend silently
 * fell back to the default and the user could never pick between app-scaffold /
 * crew-task / supervisor-alm — which behave very differently. This selector
 * fixes that: it lists blueprints with name, description and stage count, marks
 * a recommended default, and (optionally) renders a mini DAG preview of the
 * selected blueprint inline.
 *
 * Mostly presentational: the parent passes the blueprint list (api.listBlueprints)
 * and owns selection state. The mini-DAG preview is lazy — it only fetches the
 * graph for the *selected* blueprint, via the injected `loadGraph` callback, so
 * the picker stays decoupled from api.ts wiring.
 */

import { useEffect, useState } from "react";
import { clsx } from "clsx";
import { GitBranch, Layers } from "lucide-react";
import type { BlueprintGraph, BlueprintSummary } from "@/lib/api";
import { BlueprintDAG } from "@/components/blueprint-dag";
import { HelpPopover } from "@/components/guidance";

export function BlueprintPicker({
  blueprints,
  value,
  onChange,
  recommended,
  loading = false,
  /** Inject api.getBlueprintGraph so the picker can preview the selected DAG. */
  loadGraph,
  className,
}: {
  blueprints: BlueprintSummary[];
  /** Selected blueprint name (controlled). */
  value: string | null;
  onChange: (name: string) => void;
  /** Name of the repo-appropriate default, badged as "Recommended". */
  recommended?: string;
  loading?: boolean;
  loadGraph?: (name: string) => Promise<BlueprintGraph>;
  className?: string;
}) {
  const [graph, setGraph] = useState<BlueprintGraph | null>(null);
  const [graphErr, setGraphErr] = useState<string | null>(null);
  const [graphLoading, setGraphLoading] = useState(false);

  // Lazily preview the selected blueprint's DAG.
  useEffect(() => {
    if (!value || !loadGraph) {
      setGraph(null);
      return;
    }
    let cancelled = false;
    setGraph(null);
    setGraphErr(null);
    setGraphLoading(true);
    loadGraph(value)
      .then((g) => !cancelled && setGraph(g))
      .catch((e) => !cancelled && setGraphErr(String(e?.message ?? e)))
      .finally(() => !cancelled && setGraphLoading(false));
    return () => {
      cancelled = true;
    };
  }, [value, loadGraph]);

  if (loading) {
    return (
      <div className={clsx("space-y-2", className)} aria-busy>
        {[0, 1, 2].map((i) => (
          <div
            key={i}
            className="panel p-3 animate-pulse"
            style={{ height: 64, background: "var(--surface-muted)" }}
          />
        ))}
      </div>
    );
  }

  return (
    <div className={clsx("space-y-3", className)}>
      <div className="flex items-center gap-1.5">
        <span className="label-eyebrow">Blueprint</span>
        <HelpPopover term="blueprint" />
      </div>

      <ul role="radiogroup" aria-label="Blueprint" className="space-y-1.5">
        {blueprints.map((b) => {
          const selected = b.name === value;
          const isRecommended = b.name === recommended;
          return (
            <li key={b.name}>
              <button
                type="button"
                role="radio"
                aria-checked={selected}
                onClick={() => onChange(b.name)}
                className="w-full text-left rounded-md p-3 transition-colors flex items-start gap-3"
                style={{
                  background: selected ? "var(--accent-soft-bg-2)" : "var(--surface)",
                  border: `1px solid ${selected ? "var(--accent-soft-bd)" : "var(--border-subtle)"}`,
                }}
                onMouseEnter={(e) => {
                  if (!selected) e.currentTarget.style.background = "var(--surface-muted)";
                }}
                onMouseLeave={(e) => {
                  if (!selected) e.currentTarget.style.background = "var(--surface)";
                }}
              >
                <span
                  className="inline-flex items-center justify-center w-7 h-7 rounded-md shrink-0 mt-0.5"
                  style={{
                    background: selected ? "var(--accent-soft-bg)" : "var(--surface-muted)",
                    color: selected ? "var(--accent)" : "var(--ink-muted)",
                  }}
                >
                  <Layers className="w-3.5 h-3.5" />
                </span>
                <span className="min-w-0 flex-1">
                  <span className="flex items-center gap-2 flex-wrap">
                    <span
                      className="font-mono text-[13px] font-medium"
                      style={{ color: "var(--ink-strong)" }}
                    >
                      {b.title || b.name}
                    </span>
                    {isRecommended && (
                      <span className="pill pill-ok !text-[9px]">Recommended</span>
                    )}
                    <span
                      className="inline-flex items-center gap-1 text-[11px]"
                      style={{ color: "var(--ink-muted)" }}
                    >
                      <GitBranch className="w-3 h-3" />
                      {b.stageCount} stage{b.stageCount === 1 ? "" : "s"}
                    </span>
                  </span>
                  {b.description && (
                    <span
                      className="mt-0.5 block text-[12px] leading-snug line-clamp-2"
                      style={{ color: "var(--ink-soft)" }}
                    >
                      {b.description}
                    </span>
                  )}
                </span>
              </button>
            </li>
          );
        })}
      </ul>

      {blueprints.length === 0 && (
        <p className="text-[13px]" style={{ color: "var(--ink-muted)" }}>
          No blueprints available — the blueprint runtime may be disabled.
        </p>
      )}

      {/* Selected-blueprint mini DAG preview */}
      {value && loadGraph && (
        <div className="panel-muted p-3">
          <div className="label-eyebrow mb-2">Preview</div>
          {graphLoading && (
            <p className="text-[12px] animate-pulse" style={{ color: "var(--ink-muted)" }}>
              Loading {value} flow…
            </p>
          )}
          {graphErr && (
            <p className="text-[12px]" style={{ color: "var(--ink-muted)" }}>
              Preview unavailable for <span className="font-mono">{value}</span>.
            </p>
          )}
          {graph && <BlueprintDAG graph={graph} dense />}
        </div>
      )}
    </div>
  );
}
