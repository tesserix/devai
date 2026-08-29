"use client";

import { useCallback, useEffect, useState } from "react";
import { ChevronDown, ChevronRight, Send } from "lucide-react";

import { api, type SandboxInvocation, type TraceStep } from "@/lib/api";
import { traceLatencyMs, traceStepBadges } from "@/lib/sandbox-trace";

const KIND_COLOR: Record<string, string> = {
  prompt: "text-[var(--ink-500)]",
  llm: "text-indigo-300",
  tool: "text-amber-300",
  response: "text-emerald-300",
};

function text(value: unknown): string {
  if (value === null || value === undefined) return "";
  return typeof value === "string" ? value : JSON.stringify(value, null, 2);
}

function Step({ step }: { step: TraceStep }) {
  const [open, setOpen] = useState(false);
  const body = [text(step.input), text(step.output), step.error ?? ""].filter(Boolean).join("\n\n");
  const badges = traceStepBadges(step);
  return (
    <li className="border-b border-[var(--surface-border)] last:border-0">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="w-full flex items-center gap-2 px-3 py-1.5 text-left text-xs hover:bg-[var(--surface-hover)]"
      >
        {open ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
        <span className={`font-mono uppercase ${KIND_COLOR[step.kind] ?? ""}`}>{step.kind}</span>
        <span className="font-mono text-[var(--ink-100)] truncate">{step.name}</span>
        {step.mode && <span className="label-eyebrow">{step.mode}</span>}
        {badges.map((badge) => (
          <span key={badge} className="label-eyebrow">
            {badge}
          </span>
        ))}
        <span className="ml-auto flex items-center gap-3 text-[var(--ink-500)] shrink-0">
          {step.prompt_tokens + step.completion_tokens > 0 && (
            <span>
              {step.prompt_tokens}↑ {step.completion_tokens}↓
            </span>
          )}
          {step.latency_ms > 0 && <span>{step.latency_ms} ms</span>}
          {step.error && <span className="text-red-300">error</span>}
        </span>
      </button>
      {open && body && (
        <pre className="px-8 pb-2 text-[11px] font-mono text-[var(--ink-300)] whitespace-pre-wrap break-words">
          {body}
        </pre>
      )}
    </li>
  );
}

function Totals({ totals }: { totals: SandboxInvocation["totals"] }) {
  const cells: [string, string][] = [
    ["Tokens", `${totals.total_tokens}`],
    ["Wall clock", `${traceLatencyMs(totals)} ms`],
    ["Cost", `$${totals.cost_usd.toFixed(4)}`],
    ["LLM calls", `${totals.llm_calls}`],
    ["Tool calls", `${totals.tool_calls}`],
    ["Blocked", `${totals.blocked_tool_calls}`],
  ];
  return (
    <dl className="grid grid-cols-3 sm:grid-cols-6 gap-x-3 gap-y-1 text-xs px-3 py-2 border-b border-[var(--surface-border)]">
      {cells.map(([k, v]) => (
        <div key={k}>
          <dt className="label-eyebrow">{k}</dt>
          <dd className="font-mono text-[var(--ink-100)]">{v}</dd>
        </div>
      ))}
    </dl>
  );
}

export function SandboxTraceList({
  traces,
  focusedTraceId,
}: {
  traces: SandboxInvocation[];
  focusedTraceId?: string | null;
}) {
  if (traces.length === 0) {
    return (
      <p className="px-4 py-4 text-sm text-[var(--ink-500)]">
        No traces yet. Every turn records the prompt, model calls, tools, latency, tokens, and cost.
      </p>
    );
  }
  const ordered = focusedTraceId
    ? [...traces].sort((left, right) => Number(right.id === focusedTraceId) - Number(left.id === focusedTraceId))
    : traces;
  return (
    <div className="divide-y divide-[var(--surface-border)]">
      {ordered.map((trace) => (
        <article
          key={trace.id}
          id={`trace-${trace.id}`}
          className={trace.id === focusedTraceId ? "bg-indigo-500/5 ring-1 ring-inset ring-indigo-500/30" : ""}
        >
          <div className="px-4 py-2 flex items-baseline justify-between gap-3">
            <span className="text-sm text-[var(--ink-100)] truncate">{trace.message}</span>
            <span className="text-[11px] font-mono text-[var(--ink-500)] shrink-0">
              {trace.ok ? trace.id : `${trace.id} · failed`}
            </span>
          </div>
          <p className="px-4 pb-2 text-sm text-[var(--ink-50)] whitespace-pre-wrap">
            {trace.final_text || trace.error}
          </p>
          <Totals totals={trace.totals} />
          <ul>
            {trace.steps.map((step, index) => (
              <Step key={`${trace.id}-${index}`} step={step} />
            ))}
          </ul>
        </article>
      ))}
    </div>
  );
}

export function SandboxConsole({
  sandboxId,
  live,
  onTracesChange,
}: {
  sandboxId: string;
  live: boolean;
  onTracesChange?: (traces: SandboxInvocation[]) => void;
}) {
  const [traces, setTraces] = useState<SandboxInvocation[]>([]);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadTraces = useCallback(async () => {
    const found = await api.listSandboxTraces(sandboxId);
    setTraces(found);
    onTracesChange?.(found);
  }, [onTracesChange, sandboxId]);

  useEffect(() => {
    loadTraces().catch(() => setTraces([]));
  }, [loadTraces]);

  async function send() {
    if (!message.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const body = await api.invokeSandbox(sandboxId, message);
      setMessage("");
      setTraces((previous) => {
        const next = [body, ...previous];
        onTracesChange?.(next);
        return next;
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="panel p-0 overflow-hidden">
      <div className="px-4 py-2 border-b border-[var(--surface-border)]">
        <span className="label-eyebrow">Try the agent</span>
      </div>

      <div className="p-4 flex gap-2">
        <input
          value={message}
          onChange={(e) => setMessage(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && !busy && send()}
          placeholder={live ? "Ask this agent something…" : "Ask this agent something (it runs here, not in the pod)…"}
          className="flex-1 px-3 py-1.5 rounded-md border border-[var(--surface-border)] bg-[var(--surface-2)] text-sm text-[var(--ink-100)] placeholder-[var(--ink-500)] focus:outline-none focus:border-indigo-500/50"
        />
        <button
          type="button"
          onClick={send}
          disabled={busy || !message.trim()}
          className="btn-primary !py-1 !px-3 !text-xs disabled:opacity-50"
        >
          <Send className="w-3 h-3" /> {busy ? "Running…" : "Run"}
        </button>
      </div>

      {error && (
        <div className="mx-4 mb-3 rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300 font-mono">
          {error}
        </div>
      )}

      <SandboxTraceList traces={traces} />
    </section>
  );
}
