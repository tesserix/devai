"use client";

/**
 * Unified catalog page.
 *
 * aregistry (the upstream open-source agent registry, reached over
 * cluster DNS at agentregistry.agentregistry-system.svc.cluster.local)
 * only ships three tiles in its own UI — Servers / Skills / Agents — and
 * its API additionally exposes Prompts. We surface ALL of those here,
 * plus the four catalogs that DevAI owns locally (Tools, Blueprints,
 * Specializations, Stages). Eight tiles total, one click each.
 */

import { useEffect, useState } from "react";
import { PackageOpen, RefreshCw } from "lucide-react";

type Counts = { skills: number; prompts: number; mcp_servers: number; agents: number };
type LocalCounts = { tools: number; blueprints: number; specializations: number; stages: number };
type Health = { reachable: boolean; status?: string; error?: string };

type Skill = { name: string; description: string; version: string; category: string; title: string };
type Prompt = { name: string; description: string; version: string };
type McpServer = { name: string; description: string; version: string; type: string; url: string };
type Agent = {
  name: string;
  description: string;
  version: string;
  framework: string;
  language: string;
  model_provider: string;
  model_name: string;
};
type Tool = {
  name: string;
  description: string;
  category: string;
  required: string[];
  parameters: string[];
};
type Blueprint = { name: string; description: string; stage_count: number };
type Specialization = { name: string; category: string; description: string };
type Stage = { name: string };

type Tab =
  | "skills"
  | "prompts"
  | "mcp-servers"
  | "agents"
  | "tools"
  | "blueprints"
  | "specializations"
  | "stages";

const REGISTRY_TABS: Tab[] = ["skills", "prompts", "mcp-servers", "agents"];
const LOCAL_TABS: Tab[] = ["tools", "blueprints", "specializations", "stages"];

export default function RegistryPage() {
  const [counts, setCounts] = useState<Counts | null>(null);
  const [localCounts, setLocalCounts] = useState<LocalCounts | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [tab, setTab] = useState<Tab>("skills");
  // unknown[] so we don't have to spell out the union every tab switch;
  // the renderTable function downcasts based on tab.
  const [items, setItems] = useState<unknown[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function load(active: Tab) {
    setLoading(true);
    setError(null);
    try {
      // The seven API surfaces split cleanly into two prefixes: the
      // aregistry-backed routes under /api/registry/*, and the local
      // catalog routes under /api/catalog/* (+ the existing pipeline
      // blueprints endpoint and specializations route).
      const path = endpointFor(active);
      const [hRes, cRes, lcRes, iRes] = await Promise.all([
        fetch("/api/registry/health").catch(() => null),
        fetch("/api/registry/counts").catch(() => null),
        fetch("/api/catalog/counts").catch(() => null),
        fetch(path),
      ]);
      // Each response is parsed defensively: a non-2xx (e.g. an Istio/Envoy
      // 503 "upstream connect error" with a PLAIN-TEXT body) must not blow up
      // the whole page via res.json(). Health degrades to "unreachable", counts
      // degrade to empty, and only the active tab's items surface an error.
      const h = await safeJson<Health>(hRes);
      setHealth(h ?? { reachable: false, error: "registry unreachable" });

      const c = await safeJson<Counts>(cRes);
      if (c) setCounts(c);

      const lc = await safeJson<LocalCounts>(lcRes);
      if (lc) setLocalCounts(lc);

      if (iRes.ok) {
        const data = await safeJson<unknown>(iRes);
        // Some endpoints return arrays directly, some wrap them.
        const arr = Array.isArray(data)
          ? data
          : (data as { entries?: unknown[]; items?: unknown[] })?.entries ||
            (data as { items?: unknown[] })?.items ||
            [];
        setItems(arr as unknown[]);
      } else {
        const body = await iRes.text().catch(() => "");
        setError(`/${active}: HTTP ${iRes.status} — ${body.slice(0, 160) || "request failed"}`);
        setItems([]);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "fetch failed");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load(tab);
  }, [tab]);

  async function refresh() {
    // /api/registry/refresh drops the aregistry cache; local catalog
    // routes read files on each call so they don't need invalidation.
    await fetch("/api/registry/refresh", { method: "POST" }).catch(() => null);
    await load(tab);
  }

  return (
    <div className="p-7 space-y-6">
      <header className="flex items-start justify-between gap-4">
        <div>
          <div className="label-eyebrow">Catalog</div>
          <h1 className="font-serif text-2xl font-medium text-[var(--ink-50)] mt-1 flex items-center gap-2">
            <PackageOpen className="w-5 h-5 text-indigo-400" /> Agent Registry
          </h1>
          <p className="text-sm text-[var(--ink-300)] mt-1">
            Catalogue browser — aregistry-backed plus locally-declared tools, blueprints, specializations, and pipeline stages.
          </p>
          {health && (
            <p className="text-xs mt-2 flex items-center gap-2 font-mono">
              <span className={`dot ${health.reachable ? "dot-ok" : "dot-error"}`} />
              <span className={health.reachable ? "text-emerald-400" : "text-red-400"}>
                aregistry: {health.reachable ? "reachable" : "unreachable"}
              </span>
              {health.error && <span className="text-[var(--ink-500)]">· {health.error}</span>}
            </p>
          )}
        </div>
        <button
          onClick={refresh}
          className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md text-sm border border-[var(--surface-border)] bg-[var(--surface-2)] hover:bg-[var(--surface-3)] text-[var(--ink-100)] transition-colors"
        >
          <RefreshCw className="w-3.5 h-3.5" /> Refresh
        </button>
      </header>

      {/* Two strips of count cards: one for the upstream registry, one
          for locally-declared catalogs. Visually separated by a faint
          divider so the source of truth is obvious. */}
      <div>
        <div className="label-eyebrow mb-2 text-[var(--ink-400)]">Upstream — aregistry</div>
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
          <CountCard label="Skills" value={counts?.skills} active={tab === "skills"} onClick={() => setTab("skills")} />
          <CountCard label="Prompts" value={counts?.prompts} active={tab === "prompts"} onClick={() => setTab("prompts")} />
          <CountCard
            label="MCP Servers"
            value={counts?.mcp_servers}
            active={tab === "mcp-servers"}
            onClick={() => setTab("mcp-servers")}
          />
          <CountCard label="Agents" value={counts?.agents} active={tab === "agents"} onClick={() => setTab("agents")} />
        </div>
      </div>
      <div>
        <div className="label-eyebrow mb-2 text-[var(--ink-400)]">Local — devai</div>
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
          <CountCard label="Tools" value={localCounts?.tools} active={tab === "tools"} onClick={() => setTab("tools")} />
          <CountCard
            label="Blueprints"
            value={localCounts?.blueprints}
            active={tab === "blueprints"}
            onClick={() => setTab("blueprints")}
          />
          <CountCard
            label="Specializations"
            value={localCounts?.specializations}
            active={tab === "specializations"}
            onClick={() => setTab("specializations")}
          />
          <CountCard label="Stages" value={localCounts?.stages} active={tab === "stages"} onClick={() => setTab("stages")} />
        </div>
      </div>

      {error && (
        <div className="rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300 font-mono">
          {error}
        </div>
      )}

      <section className="panel overflow-hidden">
        {loading ? (
          <div className="p-6 text-sm text-[var(--ink-500)]">Loading…</div>
        ) : items.length === 0 ? (
          <div className="p-6 text-sm text-[var(--ink-500)]">No entries.</div>
        ) : (
          renderTable(tab, items)
        )}
      </section>
    </div>
  );
}

// Parse a response as JSON only when it actually succeeded AND advertises a
// JSON body. Guards against Istio/Envoy 503s whose body is plain text
// ("upstream connect error …") — calling res.json() on those throws and would
// otherwise blank the entire page.
async function safeJson<T>(res: Response | null): Promise<T | null> {
  if (!res || !res.ok) return null;
  const ct = res.headers.get("content-type") || "";
  if (!ct.includes("application/json")) return null;
  try {
    return (await res.json()) as T;
  } catch {
    return null;
  }
}

function endpointFor(tab: Tab): string {
  if (REGISTRY_TABS.includes(tab)) return `/api/registry/${tab}`;
  if (tab === "tools") return "/api/catalog/tools";
  if (tab === "stages") return "/api/catalog/stages";
  if (tab === "blueprints") return "/api/pipeline/blueprints";
  if (tab === "specializations") return "/api/specializations";
  return `/api/registry/${tab}`;
}

function CountCard({
  label,
  value,
  active,
  onClick,
}: {
  label: string;
  value?: number;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className={`text-left rounded-lg border px-4 py-3 transition-colors ${
        active
          ? "border-indigo-500/50 bg-indigo-500/10 ring-1 ring-inset ring-indigo-500/20"
          : "border-[var(--surface-border)] bg-[var(--surface-2)] hover:bg-[var(--surface-3)] hover:border-[var(--surface-border-strong)]"
      }`}
    >
      <div className="label-eyebrow">{label}</div>
      <div className="text-2xl font-mono font-medium text-[var(--ink-50)] mt-1 tabular-nums">
        {value ?? "—"}
      </div>
    </button>
  );
}

function renderTable(tab: Tab, items: unknown[]) {
  if (tab === "skills") {
    const rows = items as Skill[];
    return (
      <Table headers={["Name", "Category", "Version", "Title"]}>
        {rows.map((s) => (
          <tr key={s.name}>
            <Mono>{s.name}</Mono>
            <Cell>{s.category}</Cell>
            <Cell>{s.version}</Cell>
            <Cell>{s.title}</Cell>
          </tr>
        ))}
      </Table>
    );
  }
  if (tab === "prompts") {
    const rows = items as Prompt[];
    return (
      <Table headers={["Name", "Version", "Description"]}>
        {rows.map((p) => (
          <tr key={p.name}>
            <Mono>{p.name}</Mono>
            <Cell>{p.version}</Cell>
            <Cell>{p.description}</Cell>
          </tr>
        ))}
      </Table>
    );
  }
  if (tab === "mcp-servers") {
    const rows = items as McpServer[];
    return (
      <Table headers={["Name", "Type", "URL", "Version"]}>
        {rows.map((m) => (
          <tr key={m.name}>
            <Mono>{m.name}</Mono>
            <Cell>{m.type}</Cell>
            <MonoCell>{m.url || "—"}</MonoCell>
            <Cell>{m.version}</Cell>
          </tr>
        ))}
      </Table>
    );
  }
  if (tab === "agents") {
    const rows = items as Agent[];
    return (
      <Table headers={["Name", "Framework", "Model", "Description"]}>
        {rows.map((a) => (
          <tr key={a.name}>
            <Mono>{a.name}</Mono>
            <Cell>{a.framework}</Cell>
            <MonoCell>
              {a.model_provider}/{a.model_name}
            </MonoCell>
            <Cell>{a.description}</Cell>
          </tr>
        ))}
      </Table>
    );
  }
  if (tab === "tools") {
    const rows = items as Tool[];
    return (
      <Table headers={["Name", "Category", "Parameters", "Description"]}>
        {rows.map((t) => (
          <tr key={t.name}>
            <Mono>{t.name}</Mono>
            <Cell>{t.category}</Cell>
            <MonoCell>
              {t.parameters.map((p) => (
                <span key={p} className={t.required.includes(p) ? "text-[var(--ink-100)]" : "text-[var(--ink-500)]"}>
                  {p}
                  <span className="text-[var(--ink-500)]">{" "}</span>
                </span>
              ))}
            </MonoCell>
            <Cell>{t.description}</Cell>
          </tr>
        ))}
      </Table>
    );
  }
  if (tab === "blueprints") {
    const rows = items as Blueprint[];
    return (
      <Table headers={["Name", "Stages", "Description"]}>
        {rows.map((b) => (
          <tr key={b.name}>
            <Mono>{b.name}</Mono>
            <Cell>{b.stage_count}</Cell>
            <Cell>{b.description}</Cell>
          </tr>
        ))}
      </Table>
    );
  }
  if (tab === "specializations") {
    const rows = items as Specialization[];
    return (
      <Table headers={["Name", "Category", "Description"]}>
        {rows.map((s) => (
          <tr key={s.name}>
            <Mono>{s.name}</Mono>
            <Cell>{s.category}</Cell>
            <Cell>{s.description}</Cell>
          </tr>
        ))}
      </Table>
    );
  }
  // stages
  const rows = items as Stage[];
  return (
    <Table headers={["Name"]}>
      {rows.map((s) => (
        <tr key={s.name}>
          <Mono>{s.name}</Mono>
        </tr>
      ))}
    </Table>
  );
}

function Table({ headers, children }: { headers: string[]; children: React.ReactNode }) {
  return (
    <table className="min-w-full text-sm">
      <thead className="bg-[var(--surface-2)] text-left label-eyebrow">
        <tr>
          {headers.map((h) => (
            <th key={h} className="px-4 py-2">
              {h}
            </th>
          ))}
        </tr>
      </thead>
      <tbody className="divide-y divide-[var(--surface-border)]">{children}</tbody>
    </table>
  );
}

function Cell({ children }: { children: React.ReactNode }) {
  return <td className="px-4 py-2 text-[var(--ink-300)]">{children}</td>;
}
function Mono({ children }: { children: React.ReactNode }) {
  return <td className="px-4 py-2 font-mono text-[var(--ink-100)]">{children}</td>;
}
function MonoCell({ children }: { children: React.ReactNode }) {
  return <td className="px-4 py-2 text-[var(--ink-300)] font-mono text-xs">{children}</td>;
}
