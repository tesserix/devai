"use client";

import { useEffect, useState } from "react";
import { Cpu } from "lucide-react";
import { api, type LlmCapabilities } from "@/lib/api";

/**
 * LLM routing panel — shows the system KNOWS how it's configured: which LLM
 * providers are connected for you (your own connectors + inherited platform),
 * and how each agent role resolves to a concrete provider + model. Read-only;
 * driven by GET /api/settings/llm/capabilities. When nothing is connected it
 * tells you runs will be blocked until you add a key (matches the dispatch
 * preflight). Re-fetches when `refreshKey` changes (after saving a connector).
 */

const PROVIDER_LABELS: Record<string, string> = {
  anthropic: "Anthropic",
  openai: "OpenAI",
  vertex_gemini: "Vertex (Gemini)",
  groq: "Groq",
  gateway: "Gateway (Claude/Vertex)",
  openrouter: "OpenRouter",
};

const ROLE_LABELS: Record<string, string> = {
  dev_ui: "UI / frontend coding",
  dev_api: "Backend / API coding",
  review: "Code review & security",
  planning: "Epics & planning",
  utility: "Utility (diagnosis, runbooks)",
  boardroom_panel: "Boardroom panel",
  boardroom_moderator: "Boardroom moderator",
};

function providerLabel(p: string): string {
  return PROVIDER_LABELS[p] || p;
}

export function LlmCapabilitiesPanel({ refreshKey = 0 }: { refreshKey?: number }) {
  const [caps, setCaps] = useState<LlmCapabilities | null>(null);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let alive = true;
    api
      .llmCapabilities()
      .then((c) => {
        if (alive) setCaps(c);
      })
      .catch(() => {})
      .finally(() => {
        if (alive) setLoaded(true);
      });
    return () => {
      alive = false;
    };
  }, [refreshKey]);

  if (!loaded) return null;

  const connected = caps?.connected ?? [];
  const roles = caps?.roles ?? {};
  const none = connected.length === 0;

  return (
    <section
      className="rounded-lg border p-4"
      style={{ borderColor: "var(--accent-soft-bd)", background: "var(--surface)" }}
    >
      <div className="flex items-center gap-2 mb-1">
        <Cpu className="w-4 h-4" style={{ color: "var(--accent)" }} />
        <h3 className="text-sm font-semibold" style={{ color: "var(--ink-strong)" }}>
          LLM routing
        </h3>
      </div>
      <p className="text-xs mb-3" style={{ color: "var(--ink-muted)" }}>
        Detected from your connectors. Each agent uses the right model on a connected provider, with
        automatic fallback — Anthropic → OpenAI → Vertex → Groq.
      </p>

      {none ? (
        <div
          className="rounded-md p-3 text-[13px]"
          style={{ background: "var(--warn-soft-bg)", color: "var(--warn-ink)" }}
        >
          No LLM provider is connected. Runs are blocked until you add an LLM API key below
          (Anthropic, OpenAI, Vertex, or Groq).
        </div>
      ) : (
        <>
          <div className="flex flex-wrap items-center gap-1.5 mb-3">
            <span className="text-[11px]" style={{ color: "var(--ink-muted)" }}>
              Connected:
            </span>
            {connected.map((p, i) => (
              <span
                key={p}
                className="px-1.5 py-0.5 rounded-full text-[10px] font-semibold"
                style={
                  i === 0
                    ? { background: "var(--ok-soft-bg)", color: "var(--ok-ink)" }
                    : { background: "var(--accent-soft-bg-2)", color: "var(--accent-soft-ink)" }
                }
                title={i === 0 ? "Primary provider" : "Fallback provider"}
              >
                {providerLabel(p)}
                {i === 0 ? " · primary" : ""}
              </span>
            ))}
          </div>

          <div className="rounded-md overflow-hidden border" style={{ borderColor: "var(--border)" }}>
            {Object.entries(roles).map(([role, r], idx) => (
              <div
                key={role}
                className="flex items-center justify-between gap-2 px-3 py-1.5 text-[12px]"
                style={{
                  background: idx % 2 ? "var(--surface-muted)" : "var(--surface)",
                  color: "var(--ink)",
                }}
              >
                <span>{ROLE_LABELS[role] || role}</span>
                <span className="flex items-center gap-1.5 font-mono text-[11px]" style={{ color: "var(--ink-soft)" }}>
                  <span
                    className="px-1 py-0.5 rounded"
                    style={{ background: "var(--accent-soft-bg-2)", color: "var(--accent-soft-ink)" }}
                  >
                    {providerLabel(r.provider)}
                  </span>
                  {r.model || "default"}
                </span>
              </div>
            ))}
          </div>
        </>
      )}
    </section>
  );
}
