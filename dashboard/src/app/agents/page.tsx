"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { ExternalLink, Loader2, Pencil, Plus, Trash2, Users } from "lucide-react";
import { aregistryUrl } from "@/lib/aregistry";
import ArtifactEditor from "@/components/artifact-editor";
import { useConfirm } from "@/components/confirm-dialog";
import { GuidanceInfo, GuidancePanel } from "@/components/guidance";
import { useToast } from "@/components/toast";
import { agentLifecycle, type AgentGateStatus } from "@/lib/agent-lifecycle";
import { api } from "@/lib/api";

type Agent = {
  name: string;
  description: string;
  version: string;
  framework: string;
  language: string;
  model_provider: string;
  model_name: string;
  skills: string[];
  prompts: string[];
  labels?: Record<string, string>;
  annotations?: Record<string, string>;
};

function lifecycleFor(agent: Agent) {
  const lifecycle = agent.labels?.["devai.tesserix.app/lifecycle"] ?? "";
  const gateStatus = (agent.labels?.["devai.tesserix.app/eval-gate"] ?? "") as AgentGateStatus;
  return agentLifecycle({
    evaluationRunId: agent.annotations?.["devai.tesserix.app/eval-run-id"],
    gateStatus,
    published: true,
    running: lifecycle === "running",
  });
}

export default function AgentsPage() {
  const confirm = useConfirm();
  const toast = useToast();
  const [agents, setAgents] = useState<Agent[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [view, setView] = useState<"all" | "mine">("all");
  const [reloadKey, setReloadKey] = useState(0);
  const [editing, setEditing] = useState<{ name: string; manifest: Record<string, unknown> } | null>(null);
  const [busyAgent, setBusyAgent] = useState<{ name: string; action: "edit" | "delete" } | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setAgents([]);
    api
      .listRegistryAgents(view === "mine")
      .then((data) => {
        if (!cancelled) setAgents(data as Agent[]);
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [view, reloadKey]);

  async function editAgent(name: string) {
    setBusyAgent({ name, action: "edit" });
    setError(null);
    try {
      const manifest = await api.getOwnedRegistryAgent(name);
      setEditing({ name, manifest });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusyAgent(null);
    }
  }

  async function unpublishAgent(name: string) {
    const approved = await confirm({
      title: `Unpublish ${name}?`,
      message: "This removes the agent from your registry catalog. Existing version history may remain in registry storage.",
      confirmLabel: "Unpublish",
      tone: "danger",
    });
    if (!approved) return;
    setBusyAgent({ name, action: "delete" });
    setError(null);
    try {
      await api.unpublishArtifact("agents", name);
      toast.success(`Unpublished agent "${name}".`);
      setReloadKey((key) => key + 1);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusyAgent(null);
    }
  }

  const filtered = query
    ? agents.filter((a) => a.name.toLowerCase().includes(query.toLowerCase()))
    : agents;

  return (
    <div className="p-7 space-y-6">
      <header className="flex items-start justify-between gap-3">
        <div>
          <div className="label-eyebrow">Catalog</div>
          <h1 className="font-serif text-2xl font-medium text-[var(--ink-50)] mt-1 flex items-center gap-2">
            <Users className="w-5 h-5 text-indigo-400" /> Agents
            <GuidanceInfo id="agents" className="ml-0.5 align-middle" />
          </h1>
          <p className="text-sm text-[var(--ink-300)] mt-1">
            Agents catalogued in aregistry. Each card shows the model + framework binding the runtime will instantiate.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Filter by name…"
            className="px-3 py-1.5 rounded-md border border-[var(--surface-border)] bg-[var(--surface-2)] text-sm text-[var(--ink-100)] placeholder-[var(--ink-500)] focus:outline-none focus:border-indigo-500/50"
          />
          <Link
            href="/agents/studio"
            className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-indigo-600 hover:bg-indigo-500 text-white text-sm font-medium whitespace-nowrap"
          >
            <Plus className="w-4 h-4" /> Create Agent
          </Link>
        </div>
      </header>

      <GuidancePanel id="agents" />

      <div className="inline-flex rounded-md border border-[var(--surface-border)] bg-[var(--surface-2)] p-1" aria-label="Agent view">
        <button
          type="button"
          aria-pressed={view === "all"}
          onClick={() => setView("all")}
          className={`rounded px-3 py-1.5 text-sm transition-colors ${
            view === "all" ? "bg-indigo-600 text-white" : "text-[var(--ink-300)] hover:text-[var(--ink-100)]"
          }`}
        >
          All agents
        </button>
        <button
          type="button"
          aria-pressed={view === "mine"}
          onClick={() => setView("mine")}
          className={`rounded px-3 py-1.5 text-sm transition-colors ${
            view === "mine" ? "bg-indigo-600 text-white" : "text-[var(--ink-300)] hover:text-[var(--ink-100)]"
          }`}
        >
          My agents
        </button>
      </div>

      {error && (
        <div className="rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300 font-mono">
          {error}
        </div>
      )}

      {loading ? (
        <div className="text-sm text-[var(--ink-500)]">Loading…</div>
      ) : filtered.length === 0 ? (
        <div className="text-sm text-[var(--ink-500)]">
          {agents.length === 0
            ? view === "mine"
              ? "You have not published any agents yet."
              : "Registry returned 0 agents. Make sure the registry-bootstrap Job has completed."
            : `No agents match "${query}".`}
        </div>
      ) : (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
          {filtered.map((a) => (
            <article
              key={a.name}
              className="panel p-4 hover:border-[var(--surface-border-strong)] transition-colors group"
            >
              <div className="flex items-baseline justify-between gap-2">
                <a
                  href={aregistryUrl("agents", a.name)}
                  target="_blank"
                  rel="noreferrer"
                  title="Open in the agent registry"
                  className="text-sm font-mono font-semibold text-[var(--ink-50)] flex items-center gap-1.5"
                >
                  {a.name}
                  <ExternalLink className="w-3.5 h-3.5 text-[var(--ink-500)] opacity-0 group-hover:opacity-100" />
                </a>
                <span className="text-[11px] font-mono text-[var(--ink-500)]">v{a.version}</span>
              </div>
              <p className="text-sm text-[var(--ink-300)] mt-1">{a.description}</p>
              <div className="mt-2 flex items-center gap-2 text-[11px]">
                <span className="rounded-full border border-emerald-500/30 bg-emerald-500/10 px-2 py-0.5 capitalize text-emerald-300">
                  {lifecycleFor(a).current}
                </span>
                {a.labels?.["devai.tesserix.app/eval-gate"] && (
                  <span className="font-mono text-[var(--ink-500)]">
                    gate:{a.labels["devai.tesserix.app/eval-gate"]}
                  </span>
                )}
              </div>
              <dl className="mt-3 grid grid-cols-2 gap-x-3 gap-y-1 text-xs">
                <dt className="label-eyebrow">Model</dt>
                <dd className="font-mono text-[var(--ink-100)]">{a.model_provider}/{a.model_name}</dd>
                <dt className="label-eyebrow">Framework</dt>
                <dd className="text-[var(--ink-100)]">{a.framework}</dd>
                <dt className="label-eyebrow">Language</dt>
                <dd className="text-[var(--ink-100)]">{a.language}</dd>
              </dl>
              {view === "mine" && (
                <div className="mt-4 flex justify-end gap-2 border-t border-[var(--surface-border)] pt-3">
                  <button
                    type="button"
                    className="btn-secondary text-xs"
                    disabled={busyAgent?.name === a.name}
                    onClick={() => editAgent(a.name)}
                  >
                    {busyAgent?.name === a.name && busyAgent.action === "edit" ? (
                      <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    ) : (
                      <Pencil className="h-3.5 w-3.5" />
                    )}
                    Edit
                  </button>
                  <button
                    type="button"
                    className="btn-secondary text-xs text-red-300"
                    disabled={busyAgent?.name === a.name}
                    onClick={() => unpublishAgent(a.name)}
                  >
                    {busyAgent?.name === a.name && busyAgent.action === "delete" ? (
                      <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    ) : (
                      <Trash2 className="h-3.5 w-3.5" />
                    )}
                    Unpublish
                  </button>
                </div>
              )}
            </article>
          ))}
        </div>
      )}

      {editing && (
        <ArtifactEditor
          kind="Agent"
          open
          initialDocument={editing.manifest}
          onClose={() => setEditing(null)}
          onCreated={(name) => {
            toast.success(`Published a new version of agent "${name}".`);
            setEditing(null);
            setReloadKey((key) => key + 1);
          }}
        />
      )}
    </div>
  );
}
