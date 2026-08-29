"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import {
  Activity,
  ArrowLeft,
  Bot,
  Clock3,
  DollarSign,
  ExternalLink,
  FlaskConical,
  GitCompareArrows,
  MessageSquare,
  Plus,
  Trash2,
} from "lucide-react";

import { AgentComparisonPanel } from "@/components/agent-comparison-panel";
import { AgentEvaluationPanel } from "@/components/agent-evaluation-panel";
import { useConfirm } from "@/components/confirm-dialog";
import { GuidanceInfo, GuidancePanel, HelpPopover } from "@/components/guidance";
import { SandboxConsole, SandboxTraceList } from "@/components/sandbox-console";
import { SandboxCreateDialog } from "@/components/sandbox-create-dialog";
import { Select } from "@/components/ui/select";
import { useToast } from "@/components/toast";
import { agentSandboxes, sandboxBudget, sandboxRemainingSeconds } from "@/lib/agent-workbench";
import {
  api,
  type EvaluationDataset,
  type EvaluationRun,
  type EvaluationSuite,
  type RegistryItem,
  type SandboxInvocation,
  type SandboxRecord,
} from "@/lib/api";
import { aregistryUrl } from "@/lib/aregistry";

type AgentDetails = RegistryItem & {
  version: string;
  framework?: string;
  language?: string;
  model_provider?: string;
  model_name?: string;
};

type WorkbenchTab = "playground" | "traces" | "evaluations" | "compare";

const TABS: Array<{ id: WorkbenchTab; label: string; icon: typeof MessageSquare }> = [
  { id: "playground", label: "Playground", icon: MessageSquare },
  { id: "traces", label: "Traces", icon: Activity },
  { id: "evaluations", label: "Evaluations", icon: FlaskConical },
  { id: "compare", label: "Compare", icon: GitCompareArrows },
];

function remainingLabel(seconds: number): string {
  if (seconds <= 0) return "expired";
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  return hours > 0 ? `${hours}h ${minutes}m` : `${Math.max(1, minutes)}m`;
}

export function AgentWorkbench({ agentName }: { agentName: string }) {
  const confirm = useConfirm();
  const toast = useToast();
  const [agent, setAgent] = useState<AgentDetails | null>(null);
  const [sandboxes, setSandboxes] = useState<SandboxRecord[]>([]);
  const [selectedSandboxId, setSelectedSandboxId] = useState("");
  const [traces, setTraces] = useState<SandboxInvocation[]>([]);
  const [selectedRuns, setSelectedRuns] = useState<EvaluationRun[]>([]);
  const [allRuns, setAllRuns] = useState<EvaluationRun[]>([]);
  const [datasets, setDatasets] = useState<EvaluationDataset[]>([]);
  const [suites, setSuites] = useState<EvaluationSuite[]>([]);
  const [tab, setTab] = useState<WorkbenchTab>("playground");
  const [focusedTraceId, setFocusedTraceId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [, setClockTick] = useState(0);

  const loadPage = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [catalog, ownedSandboxes, availableDatasets, availableSuites] = await Promise.all([
        api.listRegistryAgents(),
        api.listSandboxes(),
        api.listEvaluationDatasets(),
        api.listEvaluationSuites(),
      ]);
      const found = catalog.find((item) => item.name === agentName) as AgentDetails | undefined;
      setAgent(found ?? null);
      const matching = agentSandboxes(ownedSandboxes, agentName);
      setSandboxes(matching);
      setSelectedSandboxId((current) =>
        matching.some((sandbox) => sandbox.id === current)
          ? current
          : matching.find((sandbox) => sandbox.status === "ready")?.id ?? matching[0]?.id ?? "",
      );
      setDatasets(availableDatasets);
      setSuites(availableSuites);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setLoading(false);
    }
  }, [agentName]);

  useEffect(() => {
    loadPage();
  }, [loadPage]);

  useEffect(() => {
    const timer = window.setInterval(() => setClockTick((value) => value + 1), 30_000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    if (!selectedSandboxId) {
      setTraces([]);
      setSelectedRuns([]);
      return;
    }
    let cancelled = false;
    Promise.all([
      api.listSandboxTraces(selectedSandboxId),
      api.listSandboxEvaluations(selectedSandboxId),
    ])
      .then(([foundTraces, foundRuns]) => {
        if (cancelled) return;
        setTraces(foundTraces);
        setSelectedRuns(foundRuns);
      })
      .catch((caught) => {
        if (!cancelled) setError(caught instanceof Error ? caught.message : String(caught));
      });
    return () => {
      cancelled = true;
    };
  }, [selectedSandboxId]);

  useEffect(() => {
    if (sandboxes.length === 0) {
      setAllRuns([]);
      return;
    }
    let cancelled = false;
    Promise.all(sandboxes.map((sandbox) => api.listSandboxEvaluations(sandbox.id)))
      .then((groups) => {
        if (cancelled) return;
        setAllRuns(
          groups
            .flat()
            .sort((left, right) => new Date(right.created_at).getTime() - new Date(left.created_at).getTime()),
        );
      })
      .catch((caught) => {
        if (!cancelled) setError(caught instanceof Error ? caught.message : String(caught));
      });
    return () => {
      cancelled = true;
    };
  }, [sandboxes]);

  const selectedSandbox = sandboxes.find((sandbox) => sandbox.id === selectedSandboxId) ?? null;
  const budget = useMemo(
    () =>
      selectedSandbox
        ? sandboxBudget(selectedSandbox.spec.limits.max_cost_usd, traces, selectedRuns)
        : null,
    [selectedRuns, selectedSandbox, traces],
  );
  const remaining = selectedSandbox ? sandboxRemainingSeconds(selectedSandbox.expires_at) : 0;

  const handleTracesChange = useCallback((next: SandboxInvocation[]) => setTraces(next), []);

  function handleEvaluationRun(run: EvaluationRun) {
    setSelectedRuns((current) => [run, ...current.filter((item) => item.id !== run.id)]);
    setAllRuns((current) => [run, ...current.filter((item) => item.id !== run.id)]);
  }

  async function handleCreated(id: string) {
    setCreating(false);
    try {
      const created = await api.getSandbox(id);
      setSandboxes((current) => [created, ...current.filter((sandbox) => sandbox.id !== id)]);
      setSelectedSandboxId(id);
      setTab("playground");
      toast.success(`Created sandbox ${id}.`);
    } catch {
      await loadPage();
    }
  }

  async function destroySelected() {
    if (!selectedSandbox) return;
    const approved = await confirm({
      title: `Destroy ${selectedSandbox.id}?`,
      message: "The ephemeral runtime and workspace will be removed. Durable traces, evaluations, and comparisons remain.",
      confirmLabel: "Destroy sandbox",
      tone: "danger",
    });
    if (!approved) return;
    try {
      await api.destroySandbox(selectedSandbox.id);
      const remainingSandboxes = sandboxes.filter((sandbox) => sandbox.id !== selectedSandbox.id);
      setSandboxes(remainingSandboxes);
      setSelectedSandboxId(remainingSandboxes[0]?.id ?? "");
      toast.success(`Destroyed sandbox ${selectedSandbox.id}.`);
    } catch (caught) {
      const message = caught instanceof Error ? caught.message : String(caught);
      setError(message);
      toast.error(message);
    }
  }

  async function openTrace(traceId: string) {
    setFocusedTraceId(traceId);
    setTab("traces");
    if (traces.some((trace) => trace.id === traceId)) return;
    try {
      const trace = await api.getTrace(traceId);
      if (trace.sandbox_id && trace.sandbox_id !== selectedSandboxId) {
        setSelectedSandboxId(trace.sandbox_id);
      }
      setTraces((current) => [trace, ...current.filter((item) => item.id !== trace.id)]);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    }
  }

  if (loading) return <div className="p-7 text-sm text-[var(--ink-500)]">Loading agent workbench…</div>;

  if (!agent) {
    return (
      <div className="p-7 space-y-4">
        <Link href="/agents" className="text-xs text-[var(--ink-500)] hover:text-[var(--ink-300)]">
          <ArrowLeft className="inline h-3 w-3" /> Agents
        </Link>
        <div className="panel p-4 text-sm text-[var(--ink-300)]">Agent {agentName} was not found in your catalog.</div>
      </div>
    );
  }

  return (
    <div className="p-7 space-y-5">
      <header className="flex items-start justify-between gap-4">
        <div>
          <Link href="/agents" className="inline-flex items-center gap-1 text-xs text-[var(--ink-500)] hover:text-[var(--ink-300)]">
            <ArrowLeft className="w-3 h-3" /> Agents
          </Link>
          <h1 className="mt-1 flex items-center gap-2 font-serif text-2xl font-medium text-[var(--ink-50)]">
            <Bot className="h-5 w-5 text-indigo-400" /> {agent.name}
            <GuidanceInfo id="agent-workbench" />
          </h1>
          <p className="mt-1 text-sm text-[var(--ink-300)]">
            Candidate v{agent.version} · {agent.model_provider}/{agent.model_name}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <a
            href={aregistryUrl("agents", agent.name)}
            target="_blank"
            rel="noreferrer"
            className="btn-secondary !py-1 !px-2 !text-xs"
          >
            <ExternalLink className="w-3 h-3" /> Registry
          </a>
          <button type="button" onClick={() => setCreating(true)} className="btn-primary !py-1 !px-3 !text-xs">
            <Plus className="w-3 h-3" /> Create sandbox
          </button>
        </div>
      </header>

      <GuidancePanel id="agent-workbench" />

      {error && (
        <div className="rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm font-mono text-red-300">
          {error}
        </div>
      )}

      {selectedSandbox ? (
        <section className="panel p-4" aria-label="Open sandbox status">
          <div className="flex flex-wrap items-center gap-4">
            <div className="min-w-[240px] flex-1">
              <label htmlFor="agent-sandbox" className="label-eyebrow flex items-center gap-1">
                Open sandbox <HelpPopover term="sandbox" />
              </label>
              <Select
                id="agent-sandbox"
                value={selectedSandboxId}
                onChange={setSelectedSandboxId}
                mono
                options={sandboxes.map((sandbox) => ({
                  value: sandbox.id,
                  label: `${sandbox.spec.agent.name}@${sandbox.spec.agent.version}`,
                  description: sandbox.id,
                  badge: sandbox.status,
                }))}
                ariaLabel="Open sandbox"
              />
            </div>
            <dl className="flex flex-wrap items-center gap-4 text-xs">
              <div>
                <dt className="label-eyebrow flex items-center gap-1"><Clock3 className="w-3 h-3" /> TTL</dt>
                <dd className="mt-1 font-mono text-[var(--ink-100)]">{remainingLabel(remaining)}</dd>
              </div>
              <div>
                <dt className="label-eyebrow flex items-center gap-1"><DollarSign className="w-3 h-3" /> Budget</dt>
                <dd className="mt-1 font-mono text-[var(--ink-100)]">
                  ${budget?.remainingUsd.toFixed(4)} / ${budget?.limitUsd.toFixed(2)} left
                </dd>
              </div>
              <div>
                <dt className="label-eyebrow">Tools</dt>
                <dd className="mt-1 font-mono text-[var(--ink-100)]">{selectedSandbox.spec.tools.default_mode}</dd>
              </div>
              <div>
                <dt className="label-eyebrow">Status</dt>
                <dd className="mt-1 text-[var(--ink-100)]">{selectedSandbox.status}</dd>
              </div>
            </dl>
            <button
              type="button"
              onClick={destroySelected}
              className="btn-secondary !py-1 !px-2 !text-xs text-red-300"
            >
              <Trash2 className="w-3 h-3" /> Destroy
            </button>
          </div>
        </section>
      ) : (
        <section className="panel p-5 text-center">
          <p className="text-sm text-[var(--ink-300)]">Create a sandbox to try, trace, and evaluate this agent.</p>
          <button type="button" onClick={() => setCreating(true)} className="btn-primary mt-3 !py-1 !px-3 !text-xs">
            <Plus className="w-3 h-3" /> Create sandbox
          </button>
        </section>
      )}

      <div className="border-b border-[var(--surface-border)]" role="tablist" aria-label="Agent workbench">
        {TABS.map(({ id, label, icon: Icon }) => (
          <button
            key={id}
            type="button"
            role="tab"
            aria-selected={tab === id}
            disabled={!selectedSandbox}
            onClick={() => setTab(id)}
            className={`inline-flex items-center gap-1.5 border-b-2 px-4 py-2 text-sm disabled:opacity-40 ${
              tab === id
                ? "border-indigo-400 text-[var(--ink-50)]"
                : "border-transparent text-[var(--ink-500)] hover:text-[var(--ink-300)]"
            }`}
          >
            <Icon className="w-3.5 h-3.5" /> {label}
          </button>
        ))}
      </div>

      {selectedSandbox && tab === "playground" && (
        <SandboxConsole
          key={selectedSandbox.id}
          sandboxId={selectedSandbox.id}
          live={selectedSandbox.status === "ready"}
          onTracesChange={handleTracesChange}
        />
      )}
      {selectedSandbox && tab === "traces" && (
        <section className="panel p-0 overflow-hidden">
          <div className="border-b border-[var(--surface-border)] px-4 py-2">
            <span className="label-eyebrow">Prompt → model → tool → response</span>
          </div>
          <SandboxTraceList traces={traces} focusedTraceId={focusedTraceId} />
        </section>
      )}
      {selectedSandbox && tab === "evaluations" && (
        <AgentEvaluationPanel
          sandbox={selectedSandbox}
          datasets={datasets}
          suites={suites}
          runs={selectedRuns}
          onRun={handleEvaluationRun}
          onOpenTrace={openTrace}
        />
      )}
      {selectedSandbox && tab === "compare" && (
        <AgentComparisonPanel runs={allRuns} onOpenTrace={openTrace} />
      )}

      <SandboxCreateDialog
        open={creating}
        initialAgent={agent.name}
        onClose={() => setCreating(false)}
        onCreated={(id) => void handleCreated(id)}
      />
    </div>
  );
}
