"use client";

import { useEffect, useMemo, useState } from "react";
import { Bot } from "lucide-react";
import { ModelPicker } from "@/components/model-picker";
import { Select } from "@/components/ui/select";
import { api, type EvaluationDataset, type RegistryItem, type SettingsConnector } from "@/lib/api";
import {
  canonicalSandboxProvider,
  sandboxLlmConnectorOptions,
  type SandboxConnectorOption,
} from "@/lib/sandbox-connectors";

type RegistryAgent = {
  name: string;
  version: string;
  description?: string;
  model_provider: string;
  model_name: string;
  tools?: unknown[];
  prompts?: unknown[];
};

const TOOL_MODES = [
  { value: "mock", label: "Mock", description: "Canned responses — nothing outside is touched." },
  { value: "replay", label: "Replay", description: "Recorded responses from a previous run." },
  { value: "block", label: "Block", description: "Refuse every tool call." },
  { value: "real", label: "Real", description: "Calls reach the actual system." },
];

function referenceName(reference: unknown): string {
  if (typeof reference === "string") return reference;
  if (reference && typeof reference === "object") {
    const value = reference as Record<string, unknown>;
    return String(value.name ?? value.ref ?? "");
  }
  return "";
}

function versionKey(item: { name: string; version?: string }): string {
  return `${item.name}@${item.version || "latest"}`;
}

export function SandboxCreateDialog({
  open,
  onClose,
  onCreated,
  initialAgent,
}: {
  open: boolean;
  onClose: () => void;
  onCreated: (id: string) => void;
  initialAgent?: string;
}) {
  const [agents, setAgents] = useState<RegistryAgent[]>([]);
  const [prompts, setPrompts] = useState<RegistryItem[]>([]);
  const [datasets, setDatasets] = useState<EvaluationDataset[]>([]);
  const [versions, setVersions] = useState<string[]>([]);
  const [agent, setAgent] = useState("");
  const [provider, setProvider] = useState("anthropic");
  const [model, setModel] = useState("claude-sonnet-4-20250514");
  const [adkVersion, setAdkVersion] = useState("");
  const [toolMode, setToolMode] = useState("mock");
  const [toolOverrides, setToolOverrides] = useState<Record<string, string>>({});
  const [promptKey, setPromptKey] = useState("");
  const [datasetKey, setDatasetKey] = useState("");
  const [connectorOptions, setConnectorOptions] = useState<SandboxConnectorOption[]>([]);
  const [llmConnector, setLlmConnector] = useState("");
  const [connectorConfirmed, setConnectorConfirmed] = useState(false);
  const [ttlHours, setTtlHours] = useState(4);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setConnectorConfirmed(false);
    fetch("/api/registry/agents", { credentials: "include" })
      .then((r) => (r.ok ? r.json() : []))
      .then((data: RegistryAgent[]) => {
        setAgents(data);
        if (data.length > 0) {
          setAgent((current) =>
            initialAgent && data.some((candidate) => candidate.name === initialAgent)
              ? initialAgent
              : current || data[0].name,
          );
        }
      })
      .catch(() => setAgents([]));
    // The picker offers the latest few runtime releases; the first is the default.
    fetch("/api/adk/versions", { credentials: "include" })
      .then((r) => (r.ok ? r.json() : { versions: [] }))
      .then((d: { versions: string[]; default?: string }) => {
        setVersions(d.versions ?? []);
        setAdkVersion((v) => v || d.default || d.versions?.[0] || "");
      })
      .catch(() => setVersions([]));
    api
      .listSettings()
      .then((data: { connectors: SettingsConnector[] }) => {
        const options = sandboxLlmConnectorOptions(data.connectors ?? []);
        setConnectorOptions(options);
        setLlmConnector((current) => current || options[0]?.value || "");
      })
      .catch(() => {
        setConnectorOptions([]);
        setLlmConnector("");
      });
    api.listRegistryPrompts().then(setPrompts).catch(() => setPrompts([]));
    api.listEvaluationDatasets().then(setDatasets).catch(() => setDatasets([]));
  }, [initialAgent, open]);

  // Pinning an agent pre-fills the model it was published with — still editable,
  // because comparing the same agent across models is the point of a sandbox.
  useEffect(() => {
    const chosen = agents.find((a) => a.name === agent);
    if (!chosen) return;
    if (chosen.model_provider) setProvider(chosen.model_provider);
    if (chosen.model_name) setModel(chosen.model_name);
    const compatible = connectorOptions.filter(
      (option) => option.provider === canonicalSandboxProvider(chosen.model_provider),
    );
    setLlmConnector((current) =>
      compatible.some((option) => option.value === current) ? current : compatible[0]?.value || "",
    );
    setConnectorConfirmed(false);
    setToolOverrides({});
    const preferredPrompt = referenceName(chosen.prompts?.[0]);
    const matchingPrompt = prompts.find((prompt) => prompt.name === preferredPrompt);
    setPromptKey(matchingPrompt ? versionKey(matchingPrompt) : "");
  }, [agent, agents, connectorOptions, prompts]);

  const agentOptions = useMemo(
    () =>
      agents.map((a) => ({
        value: a.name,
        label: a.name,
        description: a.description,
        badge: a.version || undefined,
      })),
    [agents],
  );

  if (!open) return null;

  const selected = agents.find((a) => a.name === agent);
  const compatibleConnectorOptions = connectorOptions.filter(
    (option) => option.provider === canonicalSandboxProvider(provider),
  );
  const selectedPrompt = prompts.find((prompt) => versionKey(prompt) === promptKey);
  const selectedDataset = datasets.find((dataset) => versionKey(dataset) === datasetKey);
  const selectedTools = (selected?.tools ?? []).map(referenceName).filter(Boolean);

  async function create() {
    setBusy(true);
    setError(null);
    try {
      const res = await fetch("/api/sandboxes", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          agent: { name: agent, version: selected?.version || "latest" },
          model: { provider, model },
          credentials: { llm_connector: llmConnector, confirmed: connectorConfirmed },
          adk_version: adkVersion || null,
          prompt: selectedPrompt ? { ref: selectedPrompt.name, version: selectedPrompt.version || "latest" } : null,
          dataset: selectedDataset ? { ref: selectedDataset.name, version: selectedDataset.version } : null,
          tools: { default_mode: toolMode, overrides: toolOverrides },
          ttl_seconds: Math.round(ttlHours * 3600),
        }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(typeof body.detail === "string" ? body.detail : `HTTP ${res.status}`);
      onCreated(body.id as string);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center px-4"
      style={{ background: "var(--surface-overlay)", backdropFilter: "blur(4px)" }}
      onClick={(e) => e.target === e.currentTarget && onClose()}
    >
      <div
        className="rounded-xl w-full max-w-2xl p-7 max-h-[88vh] overflow-y-auto"
        style={{
          background: "var(--surface-raised)",
          border: "1px solid var(--border)",
          boxShadow: "var(--shadow-raised)",
        }}
      >
        <h2 className="font-serif text-lg font-medium" style={{ color: "var(--ink-strong)" }}>
          New sandbox
        </h2>
        <p className="text-sm mt-1 mb-5" style={{ color: "var(--ink-soft)" }}>
          Pin an agent, a model and a runtime release. Everything that could change a result is fixed
          here, so two runs are comparable.
        </p>

        <div className="space-y-5">
          <div>
            <div className="flex items-baseline justify-between">
              <span className="label-eyebrow">Agent</span>
              <a href="/agents/studio" className="text-[11px] text-indigo-300 hover:underline">
                Build a new one
              </a>
            </div>
            <Select
              value={agent}
              onChange={setAgent}
              options={agentOptions}
              mono
              searchable
              placeholder={agents.length === 0 ? "No published agents" : "Choose an agent"}
              icon={<Bot className="w-3.5 h-3.5 shrink-0" style={{ color: "var(--ink-muted)" }} />}
              ariaLabel="Agent"
            />
          </div>

          <ModelPicker
            provider={provider}
            model={model}
            onChange={(next) => {
              setProvider(next.provider);
              setModel(next.model);
              const compatible = connectorOptions.filter(
                (option) => option.provider === canonicalSandboxProvider(next.provider),
              );
              setLlmConnector(compatible[0]?.value || "");
              setConnectorConfirmed(false);
            }}
          />

          <div className="grid grid-cols-2 gap-3">
            <div>
              <label htmlFor="sb-prompt" className="label-eyebrow">Prompt version</label>
              <Select
                id="sb-prompt"
                value={promptKey}
                onChange={setPromptKey}
                options={[
                  { value: "", label: "Agent default", description: "Use the prompt pinned by the Agent artifact" },
                  ...prompts.map((prompt) => ({
                    value: versionKey(prompt),
                    label: versionKey(prompt),
                    description: prompt.description,
                  })),
                ]}
                searchable
                mono
                ariaLabel="Prompt version"
              />
            </div>
            <div>
              <label htmlFor="sb-dataset" className="label-eyebrow">Dataset version</label>
              <Select
                id="sb-dataset"
                value={datasetKey}
                onChange={setDatasetKey}
                options={[
                  { value: "", label: "No dataset", description: "Attach one later when running an evaluation" },
                  ...datasets.map((dataset) => ({
                    value: versionKey(dataset),
                    label: versionKey(dataset),
                    description: `${dataset.case_count} cases · ${dataset.description}`,
                  })),
                ]}
                searchable
                mono
                ariaLabel="Dataset version"
              />
            </div>
          </div>

          <div>
            <label htmlFor="sb-llm-connector" className="label-eyebrow">
              Sandbox LLM credential
            </label>
            <Select
              id="sb-llm-connector"
              value={llmConnector}
              onChange={(value) => {
                setLlmConnector(value);
                setConnectorConfirmed(false);
              }}
              options={compatibleConnectorOptions}
              placeholder={compatibleConnectorOptions.length === 0 ? `No personal ${provider} connector` : "Choose a connector"}
              ariaLabel="Sandbox LLM credential"
            />
            {compatibleConnectorOptions.length === 0 && (
              <p className="text-xs mt-1.5" style={{ color: "var(--ink-muted)" }}>
                Add a personal connector with its own key in <a href="/settings" className="text-indigo-300 hover:underline">Settings</a>.
                Shared team, tenant and platform credentials cannot enter a sandbox.
              </p>
            )}
          </div>

          <label className="flex items-start gap-2 text-xs" style={{ color: "var(--ink-soft)" }}>
            <input
              type="checkbox"
              checked={connectorConfirmed}
              disabled={!llmConnector}
              onChange={(event) => setConnectorConfirmed(event.target.checked)}
              className="mt-0.5"
            />
            <span>
              I authorize this sandbox to use the selected connector. Its key stays in the DevAI control plane,
              calls go through AgentGateway, and no SCM, cloud or platform credential is inherited.
            </span>
          </label>

          <div className="grid grid-cols-2 gap-3">
            <div>
              <label htmlFor="sb-adk" className="label-eyebrow">
                Runtime (ADK)
              </label>
              <Select
                id="sb-adk"
                value={adkVersion}
                onChange={setAdkVersion}
                mono
                placeholder="default"
                options={versions.map((v, i) => ({
                  value: v,
                  label: v,
                  badge: i === 0 ? "latest" : undefined,
                }))}
                ariaLabel="Runtime version"
              />
            </div>
            <div>
              <label htmlFor="sb-ttl" className="label-eyebrow">
                Expires in (hours)
              </label>
              <input
                id="sb-ttl"
                type="number"
                min={1}
                max={24}
                value={ttlHours}
                onChange={(e) => setTtlHours(Number(e.target.value))}
                className="field w-full"
              />
            </div>
          </div>

          <div>
            <label htmlFor="sb-tools" className="label-eyebrow">
              Tools
            </label>
            <Select id="sb-tools" value={toolMode} onChange={setToolMode} options={TOOL_MODES} ariaLabel="Tool mode" />
            {selectedTools.length > 0 && (
              <div className="mt-2 grid grid-cols-2 gap-2">
                {selectedTools.map((tool) => (
                  <div key={tool}>
                    <label htmlFor={`sb-tool-${tool}`} className="text-[11px] font-mono text-[var(--ink-500)]">
                      {tool}
                    </label>
                    <Select
                      id={`sb-tool-${tool}`}
                      value={toolOverrides[tool] ?? ""}
                      onChange={(mode) =>
                        setToolOverrides((current) => {
                          if (!mode) {
                            const next = { ...current };
                            delete next[tool];
                            return next;
                          }
                          return { ...current, [tool]: mode };
                        })
                      }
                      options={[
                        { value: "", label: `Default (${toolMode})` },
                        ...TOOL_MODES,
                      ]}
                      ariaLabel={`${tool} mode`}
                    />
                  </div>
                ))}
              </div>
            )}
          </div>

          {error && (
            <div className="rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300 font-mono">
              {error}
            </div>
          )}

          <div className="flex justify-end gap-2 pt-1">
            <button type="button" onClick={onClose} className="btn-secondary !py-1 !px-3 !text-xs">
              Cancel
            </button>
            <button
              type="button"
              onClick={create}
              disabled={busy || !agent || !llmConnector || !connectorConfirmed}
              className="btn-primary !py-1 !px-3 !text-xs disabled:opacity-50"
            >
              {busy ? "Creating…" : "Create sandbox"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
