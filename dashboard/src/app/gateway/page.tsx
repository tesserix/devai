"use client";

import { useCallback, useEffect, useState } from "react";
import {
  Activity,
  CheckCircle2,
  CircleDashed,
  RefreshCw,
  Send,
  ShieldHalf,
  XCircle,
} from "lucide-react";
import { GuidanceInfo, GuidancePanel } from "@/components/guidance";

/**
 * Gateway panel — single-page status board for the agentic control
 * plane (registry / agentgateway / ai-gateway / kagent). Hits
 * /api/agentic/status which fans out to the four in-cluster
 * services. A 'Test LLM' button runs /api/agentic/llm-probe to
 * confirm the LLM data plane round-trips through agentgateway-system.
 *
 * Theme: tokens only — light + dark identical structure, just
 * different surfaces. No raw Tailwind grays.
 */
type ComponentStatus = {
  name: string;
  role: "registry" | "gateway" | "controller" | "llm-proxy";
  namespace: string;
  url: string;
  reachable: boolean;
  detail: Record<string, unknown>;
  error: string;
};

type AgenticStatusJson = {
  registry: ComponentStatus;
  agentgateway: ComponentStatus;
  ai_gateway: ComponentStatus;
  kagent: ComponentStatus;
  catalog: Record<string, number>;
};

type LLMProbe = {
  ok: boolean;
  adapter?: string;
  provider?: string;
  model?: string;
  text?: string;
  usage?: { input: number; output: number };
  error?: string;
};

export default function GatewayPage() {
  const [status, setStatus] = useState<AgenticStatusJson | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [probe, setProbe] = useState<LLMProbe | null>(null);
  const [probing, setProbing] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const r = await fetch("/api/agentic/status");
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      setStatus(await r.json());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function runProbe() {
    setProbing(true);
    setProbe(null);
    try {
      const r = await fetch("/api/agentic/llm-probe");
      setProbe(await r.json());
    } catch (e) {
      setProbe({ ok: false, error: e instanceof Error ? e.message : String(e) });
    } finally {
      setProbing(false);
    }
  }

  return (
    <div className="p-7 space-y-6">
      <header className="flex items-start justify-between gap-4">
        <div>
          <div className="label-eyebrow">Control plane</div>
          <h1 className="font-serif text-2xl font-medium mt-1 flex items-center gap-2" style={{ color: "var(--ink-strong)" }}>
            <ShieldHalf className="w-5 h-5" style={{ color: "var(--accent)" }} /> Agent Gateway
            <GuidanceInfo id="gateway" className="ml-0.5 align-middle" />
          </h1>
          <p className="text-sm mt-1" style={{ color: "var(--ink-soft)" }}>
            Live status of the agentic stack — agentregistry, Solo.io agentgateway, the LLM proxy, and kagent. All
            traffic from DevAI to external LLMs flows via this namespace.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button onClick={runProbe} disabled={probing} className="btn-secondary">
            <Send className="w-3.5 h-3.5" /> {probing ? "Testing…" : "Test LLM"}
          </button>
          <button onClick={load} className="btn-secondary">
            <RefreshCw className="w-3.5 h-3.5" /> Refresh
          </button>
        </div>
      </header>

      <GuidancePanel id="gateway" />

      {error && (
        <div
          className="rounded-md px-3 py-2 text-sm font-mono"
          style={{
            background: "var(--error-soft-bg)",
            color: "var(--error-ink)",
            border: "1px solid var(--error-soft-bd)",
          }}
        >
          {error}
        </div>
      )}

      {/* Component cards */}
      {loading ? (
        <div className="text-sm" style={{ color: "var(--ink-muted)" }}>Loading…</div>
      ) : !status ? null : (
        <section className="grid grid-cols-1 md:grid-cols-2 gap-3">
          <ComponentCard component={status.registry} />
          <ComponentCard component={status.agentgateway} />
          <ComponentCard component={status.ai_gateway} />
          <ComponentCard component={status.kagent} />
        </section>
      )}

      {/* Probe result */}
      {probe && (
        <section className="panel p-4">
          <div className="label-eyebrow mb-2">LLM probe</div>
          {probe.ok ? (
            <div className="space-y-2 text-sm">
              <div className="flex items-center gap-2">
                <CheckCircle2 className="w-4 h-4" style={{ color: "var(--ok)" }} />
                <span style={{ color: "var(--ink-strong)" }}>Round-trip succeeded</span>
              </div>
              <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
                <dt className="label-eyebrow">Adapter</dt>
                <dd className="font-mono" style={{ color: "var(--ink)" }}>{probe.adapter}</dd>
                <dt className="label-eyebrow">Provider</dt>
                <dd className="font-mono" style={{ color: "var(--ink)" }}>{probe.provider}</dd>
                <dt className="label-eyebrow">Model</dt>
                <dd className="font-mono" style={{ color: "var(--ink)" }}>{probe.model}</dd>
                <dt className="label-eyebrow">Tokens</dt>
                <dd className="font-mono" style={{ color: "var(--ink)" }}>
                  in: {probe.usage?.input ?? "?"} · out: {probe.usage?.output ?? "?"}
                </dd>
              </dl>
              <blockquote
                className="mt-2 px-3 py-2 rounded-md font-serif italic text-sm"
                style={{ background: "var(--surface-muted)", color: "var(--ink-strong)" }}
              >
                “{probe.text}”
              </blockquote>
            </div>
          ) : (
            <div className="flex items-start gap-2 text-sm">
              <XCircle className="w-4 h-4 mt-0.5" style={{ color: "var(--error)" }} />
              <div>
                <div style={{ color: "var(--ink-strong)" }}>Probe failed</div>
                <div className="font-mono text-xs mt-1" style={{ color: "var(--ink-soft)" }}>{probe.error}</div>
              </div>
            </div>
          )}
        </section>
      )}

      {/* Catalog counts (mirrors Registry page but rendered here too
       * so 'Gateway' is a useful health page on its own). */}
      {status && status.catalog && Object.keys(status.catalog).length > 0 && (
        <section>
          <div className="label-eyebrow mb-2">Catalogue</div>
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
            {Object.entries(status.catalog).map(([k, v]) => (
              <div key={k} className="panel p-4">
                <div className="label-eyebrow capitalize">{k.replace(/_/g, " ")}</div>
                <div className="text-2xl font-mono font-medium mt-1 tabular-nums" style={{ color: "var(--ink-strong)" }}>
                  {v}
                </div>
              </div>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}

function ComponentCard({ component }: { component: ComponentStatus }) {
  const tone = component.reachable ? "ok" : "error";
  const Icon = component.reachable ? CheckCircle2 : component.error ? XCircle : CircleDashed;
  const roleLabel: Record<ComponentStatus["role"], string> = {
    registry: "Catalog",
    gateway: "Gateway",
    controller: "Controller",
    "llm-proxy": "LLM Proxy",
  };
  return (
    <article
      className="panel p-4 transition-colors"
      style={{ borderColor: component.reachable ? "var(--ok-soft-bd)" : "var(--border-subtle)" }}
    >
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2 min-w-0">
          <Icon
            className="w-4 h-4 shrink-0"
            style={{ color: tone === "ok" ? "var(--ok)" : "var(--error)" }}
          />
          <h3 className="text-sm font-semibold truncate" style={{ color: "var(--ink-strong)" }}>
            {component.name}
          </h3>
        </div>
        <span className={`pill ${component.reachable ? "pill-ok" : "pill-error"}`}>
          {component.reachable ? "online" : "offline"}
        </span>
      </div>
      <dl className="mt-3 grid grid-cols-[110px_1fr] gap-x-3 gap-y-1.5 text-xs">
        <dt className="label-eyebrow">Role</dt>
        <dd style={{ color: "var(--ink)" }}>{roleLabel[component.role] ?? component.role}</dd>
        <dt className="label-eyebrow">Namespace</dt>
        <dd className="font-mono" style={{ color: "var(--ink)" }}>{component.namespace}</dd>
        <dt className="label-eyebrow">Endpoint</dt>
        <dd className="font-mono break-all" style={{ color: "var(--ink-soft)" }}>{component.url || "—"}</dd>
        {component.error && (
          <>
            <dt className="label-eyebrow">Error</dt>
            <dd className="font-mono" style={{ color: "var(--error-ink)" }}>{component.error}</dd>
          </>
        )}
        {!component.error && component.detail && Object.keys(component.detail).length > 0 && (
          <>
            <dt className="label-eyebrow">Detail</dt>
            <dd className="font-mono text-[11px] break-all" style={{ color: "var(--ink-soft)" }}>
              {JSON.stringify(component.detail)}
            </dd>
          </>
        )}
      </dl>
    </article>
  );
}

// Activity import kept so future SSE work has a place to grab the icon.
void Activity;
