"use client";

import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { Bot, Check, Loader2 } from "lucide-react";

import { api, type CatalogTool, type RegistryItem } from "@/lib/api";

const PROVIDERS = ["auto", "claude", "openai", "codex", "groq", "gemini", "nemoclaw"];
const CATEGORIES = ["planning", "coding", "review", "orchestration", "sre", "specialist"];
const RISK_LEVELS = ["low", "medium", "high", "critical"];

/**
 * Create Agent — author a custom specialization: name it, pick the LLM,
 * select tools from the catalog, write the prompt. Saving POSTs to
 * /api/authoring/specializations, which validates and registers the agent
 * live so it's immediately usable as a `run_specialization` stage.
 */
export default function NewAgentPage() {
  const router = useRouter();

  const [tools, setTools] = useState<CatalogTool[]>([]);
  const [name, setName] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [description, setDescription] = useState("");
  const [category, setCategory] = useState("specialist");
  const [provider, setProvider] = useState("claude");
  const [model, setModel] = useState("");
  const [risk, setRisk] = useState("medium");
  const [systemPrompt, setSystemPrompt] = useState("");
  const [selectedTools, setSelectedTools] = useState<Set<string>>(new Set());
  const [toolFilter, setToolФильтр] = useState(""); // search box

  // Registry catalog — skills/prompts/MCP servers composed into this agent.
  const [skillsCat, setSkillsCat] = useState<RegistryItem[]>([]);
  const [promptsCat, setPromptsCat] = useState<RegistryItem[]>([]);
  const [mcpCat, setMcpCat] = useState<RegistryItem[]>([]);
  const [selSkills, setSelSkills] = useState<Set<string>>(new Set());
  const [selPrompts, setSelPrompts] = useState<Set<string>>(new Set());
  const [selMcp, setSelMcp] = useState<Set<string>>(new Set());

  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);

  useEffect(() => {
    api
      .listCatalogTools()
      .then(setTools)
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
    // Registry catalog is best-effort — a registry-less dev cluster just
    // shows empty pickers (built-in tools still work).
    api.listRegistrySkills().then(setSkillsCat).catch(() => {});
    api.listRegistryPrompts().then(setPromptsCat).catch(() => {});
    api.listRegistryMcpServers().then(setMcpCat).catch(() => {});
  }, []);

  const filteredTools = useMemo(() => {
    const q = toolFilter.trim().toLowerCase();
    return q ? tools.filter((t) => `${t.name} ${t.description}`.toLowerCase().includes(q)) : tools;
  }, [tools, toolFilter]);

  const nameValid = /^[a-z][a-z0-9_]{1,63}$/.test(name);

  function toggleTool(tool: string) {
    setSelectedTools((prev) => {
      const next = new Set(prev);
      if (next.has(tool)) next.delete(tool);
      else next.add(tool);
      return next;
    });
  }

  async function save() {
    setError(null);
    setDone(null);
    if (!nameValid) {
      setError("Name must be lowercase snake_case (e.g. my_agent), 2–64 chars.");
      return;
    }
    if (!systemPrompt.trim()) {
      setError("A system prompt is required.");
      return;
    }
    setSaving(true);
    try {
      const res = await api.createAgent({
        name,
        display_name: displayName || name,
        description,
        category,
        llm_provider: provider,
        llm_model: model || undefined,
        risk_level: risk,
        system_prompt: systemPrompt,
        allowed_tools: [...selectedTools],
        skills: [...selSkills],
        prompts: [...selPrompts],
        mcp_servers: [...selMcp],
      });
      setDone(res.name);
      setTimeout(() => router.push("/agents"), 900);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="p-7 space-y-6 max-w-4xl">
      <header>
        <div className="label-eyebrow">Authoring</div>
        <h1 className="font-serif text-2xl font-medium text-[var(--ink-50)] mt-1 flex items-center gap-2">
          <Bot className="w-5 h-5 text-indigo-400" /> Create Agent
        </h1>
        <p className="text-sm text-[var(--ink-300)] mt-1">
          Define a custom agent, pick the tools it can call, and write its prompt. It becomes runnable
          immediately as a <code className="font-mono">run_specialization</code> stage in any blueprint.
        </p>
      </header>

      {error && (
        <div className="rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300 font-mono">
          {error}
        </div>
      )}
      {done && (
        <div className="rounded-md border border-emerald-500/30 bg-emerald-500/10 px-3 py-2 text-sm text-emerald-300 flex items-center gap-2">
          <Check className="w-4 h-4" /> Created agent <span className="font-mono">{done}</span> — redirecting…
        </div>
      )}

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <Field label="Name (snake_case)">
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="my_agent"
            className={inputCls + (name && !nameValid ? " border-red-500/50" : "")}
          />
        </Field>
        <Field label="Display name">
          <input value={displayName} onChange={(e) => setDisplayName(e.target.value)} placeholder="My Agent" className={inputCls} />
        </Field>
        <Field label="Category">
          <select value={category} onChange={(e) => setCategory(e.target.value)} className={inputCls}>
            {CATEGORIES.map((c) => (
              <option key={c} value={c}>{c}</option>
            ))}
          </select>
        </Field>
        <Field label="Risk level">
          <select value={risk} onChange={(e) => setRisk(e.target.value)} className={inputCls}>
            {RISK_LEVELS.map((r) => (
              <option key={r} value={r}>{r}</option>
            ))}
          </select>
        </Field>
        <Field label="LLM provider">
          <select value={provider} onChange={(e) => setProvider(e.target.value)} className={inputCls}>
            {PROVIDERS.map((p) => (
              <option key={p} value={p}>{p}</option>
            ))}
          </select>
        </Field>
        <Field label="Model (optional)">
          <input value={model} onChange={(e) => setModel(e.target.value)} placeholder="provider default" className={inputCls} />
        </Field>
      </div>

      <Field label="Description">
        <input value={description} onChange={(e) => setDescription(e.target.value)} placeholder="What this agent does" className={inputCls} />
      </Field>

      <Field label="System prompt">
        <textarea
          value={systemPrompt}
          onChange={(e) => setSystemPrompt(e.target.value)}
          rows={8}
          placeholder="You are a … Your job is to …"
          className={inputCls + " font-mono text-xs leading-relaxed"}
        />
      </Field>

      <div>
        <div className="flex items-center justify-between mb-2">
          <label className="label-eyebrow">Tools ({selectedTools.size} selected)</label>
          <input
            value={toolFilter}
            onChange={(e) => setToolФильтр(e.target.value)}
            placeholder="Filter tools…"
            className="px-3 py-1.5 rounded-md border border-[var(--surface-border)] bg-[var(--surface-2)] text-sm text-[var(--ink-100)] placeholder-[var(--ink-500)] focus:outline-none focus:border-indigo-500/50"
          />
        </div>
        <div className="panel p-2 max-h-72 overflow-y-auto grid grid-cols-1 sm:grid-cols-2 gap-1">
          {filteredTools.length === 0 ? (
            <div className="text-sm text-[var(--ink-500)] p-2">No tools.</div>
          ) : (
            filteredTools.map((t) => {
              const on = selectedTools.has(t.name);
              return (
                <button
                  key={t.name}
                  type="button"
                  onClick={() => toggleTool(t.name)}
                  className={
                    "text-left px-2.5 py-1.5 rounded-md border transition-colors " +
                    (on
                      ? "border-indigo-500/60 bg-indigo-500/10"
                      : "border-[var(--surface-border)] hover:border-[var(--surface-border-strong)]")
                  }
                >
                  <div className="flex items-center gap-2">
                    <span className={"w-3 h-3 rounded-sm border " + (on ? "bg-indigo-500 border-indigo-500" : "border-[var(--ink-500)]")} />
                    <span className="font-mono text-xs text-[var(--ink-100)]">{t.name}</span>
                    <span className="text-[10px] text-[var(--ink-500)] ml-auto">{t.category}</span>
                  </div>
                  <p className="text-[11px] text-[var(--ink-400)] mt-0.5 line-clamp-1">{t.description}</p>
                </button>
              );
            })
          )}
        </div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <CatalogPicker
          label="Skills"
          items={skillsCat}
          selected={selSkills}
          onToggle={(n) => toggleIn(setSelSkills, n)}
          emptyHint="No skills in the registry."
        />
        <CatalogPicker
          label="Prompts"
          items={promptsCat}
          selected={selPrompts}
          onToggle={(n) => toggleIn(setSelPrompts, n)}
          emptyHint="No prompts in the registry."
        />
        <CatalogPicker
          label="MCP Servers"
          items={mcpCat}
          selected={selMcp}
          onToggle={(n) => toggleIn(setSelMcp, n)}
          emptyHint="No MCP servers in the registry."
        />
      </div>

      <p className="text-[11px] text-[var(--ink-500)]">
        Skills, prompts and MCP servers are pulled from the shared registry and published back with this
        agent, so it’s discoverable and runnable across the platform.
      </p>

      <div className="flex items-center gap-3">
        <button
          type="button"
          onClick={save}
          disabled={saving}
          className="inline-flex items-center gap-2 px-4 py-2 rounded-md bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 text-white text-sm font-medium"
        >
          {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Bot className="w-4 h-4" />}
          Create agent
        </button>
        <button
          type="button"
          onClick={() => router.push("/agents")}
          className="px-4 py-2 rounded-md border border-[var(--surface-border)] text-sm text-[var(--ink-200)] hover:border-[var(--surface-border-strong)]"
        >
          Cancel
        </button>
      </div>
    </div>
  );
}

const inputCls =
  "w-full px-3 py-2 rounded-md border border-[var(--surface-border)] bg-[var(--surface-2)] text-sm text-[var(--ink-100)] placeholder-[var(--ink-500)] focus:outline-none focus:border-indigo-500/50";

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block space-y-1.5">
      <span className="label-eyebrow">{label}</span>
      {children}
    </label>
  );
}

function toggleIn(setter: React.Dispatch<React.SetStateAction<Set<string>>>, name: string) {
  setter((prev) => {
    const next = new Set(prev);
    if (next.has(name)) next.delete(name);
    else next.add(name);
    return next;
  });
}

/** A compact multi-select list backed by a registry collection. */
function CatalogPicker({
  label,
  items,
  selected,
  onToggle,
  emptyHint,
}: {
  label: string;
  items: RegistryItem[];
  selected: Set<string>;
  onToggle: (name: string) => void;
  emptyHint: string;
}) {
  return (
    <div>
      <label className="label-eyebrow">
        {label} ({selected.size})
      </label>
      <div className="panel p-2 mt-2 max-h-56 overflow-y-auto space-y-1">
        {items.length === 0 ? (
          <div className="text-xs text-[var(--ink-500)] p-2">{emptyHint}</div>
        ) : (
          items.map((it) => {
            const on = selected.has(it.name);
            const title = it.title || it.display_name || it.name;
            return (
              <button
                key={it.name}
                type="button"
                onClick={() => onToggle(it.name)}
                className={
                  "w-full text-left px-2.5 py-1.5 rounded-md border transition-colors " +
                  (on
                    ? "border-indigo-500/60 bg-indigo-500/10"
                    : "border-[var(--surface-border)] hover:border-[var(--surface-border-strong)]")
                }
              >
                <div className="flex items-center gap-2">
                  <span
                    className={
                      "w-3 h-3 rounded-sm border " +
                      (on ? "bg-indigo-500 border-indigo-500" : "border-[var(--ink-500)]")
                    }
                  />
                  <span className="font-mono text-xs text-[var(--ink-100)] truncate">{it.name}</span>
                </div>
                {title !== it.name && (
                  <p className="text-[11px] text-[var(--ink-400)] mt-0.5 line-clamp-1">{title}</p>
                )}
              </button>
            );
          })
        )}
      </div>
    </div>
  );
}
