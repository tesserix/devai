"use client";

import { useMemo, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ArrowLeft, ArrowRight, Bot, Check, FlaskConical, Rocket } from "lucide-react";

import { EvalPanel, type EvalCase } from "@/components/eval-panel";
import { ModelCompare } from "@/components/model-compare";
import { ModelPicker } from "@/components/model-picker";
import { SandboxConsole } from "@/components/sandbox-console";
import { useToast } from "@/components/toast";
import { api } from "@/lib/api";
import { lintManifest } from "@/lib/registry-schemas";

/**
 * Agent Studio — define an agent, run it in a sandbox, then publish it.
 *
 * The middle step is the reason this page exists separately from the manifest
 * editor: the draft definition is pinned into a sandbox and invoked before it
 * ever reaches the catalog, so nothing is published untested.
 */

const STEPS = ["Define", "Test", "Publish"] as const;

/** Starter definitions — a blank system prompt is the slowest way to begin. */
const TEMPLATES: { label: string; title: string; prompt: string; tools: string; check: EvalCase }[] = [
  {
    label: "Release notes",
    title: "Release Notes Writer",
    prompt:
      "You are a release manager. Given a diff or a list of merged PRs, write release notes for customers: " +
      "one line per user-visible change, grouped as Added / Changed / Fixed. Never invent a change.",
    tools: "scm_list_files, scm_read_file",
    check: {
      name: "groups the changes",
      input: "Merged: add sandbox console, fix token counter",
      expect: { contains: ["Added", "Fixed"] },
    },
  },
  {
    label: "Code reviewer",
    title: "Code Reviewer",
    prompt:
      "You review diffs. Report only defects you can point at: correctness, security, and data loss first, " +
      "style last. If the diff is fine, say so in one line rather than inventing findings.",
    tools: "scm_list_files, scm_read_file",
    check: {
      name: "flags a hardcoded secret",
      input: "Review: +const token = 'ghp_live_abc123'",
      expect: { contains: ["secret"] },
    },
  },
  {
    label: "Incident responder",
    title: "Incident Responder",
    prompt:
      "You triage production alerts. State the likely blast radius, the most probable cause, and the next " +
      "diagnostic step. Never propose a change that cannot be rolled back in one action.",
    tools: "k8s_get_pods, k8s_logs",
    check: {
      name: "names a next step",
      input: "CrashLoopBackOff on devai-api, 3 restarts in 5 minutes",
      expect: { contains: ["logs"] },
    },
  },
];

function slug(value: string): string {
  return value.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 253);
}

/** Which spec fields this version would change, so publishing over an existing
 *  agent is a decision rather than a surprise. */
function changedFields(
  before: Record<string, unknown>,
  after: Record<string, unknown>,
): { key: string; before: string; after: string }[] {
  const show = (v: unknown) => (v === undefined ? "—" : typeof v === "string" ? v : JSON.stringify(v));
  return [...new Set([...Object.keys(before), ...Object.keys(after)])]
    .filter((k) => JSON.stringify(before[k] ?? null) !== JSON.stringify(after[k] ?? null))
    .map((k) => ({ key: k, before: show(before[k]), after: show(after[k]) }));
}

function list(value: string): string[] {
  return value.split(/[\s,]+/).map((v) => v.trim()).filter(Boolean);
}

export default function AgentStudioPage() {
  const router = useRouter();
  const toast = useToast();

  const [step, setStep] = useState(0);
  const [title, setTitle] = useState("");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [systemPrompt, setSystemPrompt] = useState("");
  const [provider, setProvider] = useState("anthropic");
  const [model, setModel] = useState("claude-sonnet-4-6");
  const [tools, setTools] = useState("");
  const [skills, setSkills] = useState("");
  const [maxTurns, setMaxTurns] = useState(8);
  const [cases, setCases] = useState<EvalCase[]>([]);

  const [sandboxId, setSandboxId] = useState("");
  const [testedDefinition, setTestedDefinition] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [published, setPublished] = useState("");
  const [conflict, setConflict] = useState<Record<string, unknown> | null>(null);

  const agentName = name || slug(title);

  const manifest = useMemo(
    () => ({
      apiVersion: "registry.agentic.dev/v1alpha1",
      kind: "Agent",
      metadata: { name: agentName, visibility: "private", labels: {} },
      spec: {
        title: title || agentName,
        description,
        model: { provider, name: model },
        systemPrompt,
        builtinTools: list(tools),
        skills: list(skills),
        maxTurns,
        // Checks travel with the definition, so a published agent can be
        // re-tested later without anyone remembering what it was for.
        evals: cases,
      },
    }),
    [agentName, title, description, provider, model, systemPrompt, tools, skills, maxTurns, cases],
  );

  const issues = lintManifest(manifest, "Agent");
  const blocking = issues.filter((i) => i.level === "error");
  const definable = Boolean(agentName && systemPrompt.trim()) && blocking.length === 0;
  // A sandbox pins the definition it was created with, so an edited draft needs
  // a fresh one — otherwise the trace answers a question you stopped asking.
  // Checks are not pinned: adding one is a new question about the same agent.
  const pinned = JSON.stringify({ ...manifest.spec, evals: undefined });
  const stale = Boolean(sandboxId) && testedDefinition !== pinned;

  async function startTest() {
    setBusy(true);
    setError(null);
    try {
      const res = await fetch("/api/sandboxes", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          agent: { name: agentName, version: "draft" },
          model: { provider, model },
          draft: manifest,
          tools: { default_mode: "mock" },
          ttl_seconds: 3600,
        }),
      });
      const created = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(typeof created.detail === "string" ? created.detail : `HTTP ${res.status}`);
      setSandboxId(created.id as string);
      setTestedDefinition(pinned);
      setStep(1);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function publish(overwrite = false) {
    setBusy(true);
    setError(null);
    try {
      const res = await fetch(`/api/registry/agents${overwrite ? "?overwrite=true" : ""}`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(manifest),
      });
      // A name that already exists is not an error — it is the next version of
      // an agent, so show what would change instead of a red box.
      if (res.status === 409) {
        const current = await fetch(`/api/registry/agents/${encodeURIComponent(agentName)}`, {
          credentials: "include",
        })
          .then((r) => (r.ok ? r.json() : null))
          .catch(() => null);
        setConflict(current?.spec ?? current ?? {});
        return;
      }
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(typeof body.detail === "string" ? body.detail : `HTTP ${res.status}`);
      }
      setConflict(null);
      setPublished(agentName);
      toast.success(`Published agent "${agentName}" to the registry.`);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  const field =
    "w-full px-3 py-1.5 rounded-md border border-[var(--surface-border)] bg-[var(--surface-2)] text-sm text-[var(--ink-100)] placeholder-[var(--ink-500)] focus:outline-none focus:border-indigo-500/50";

  return (
    <div className="p-7 space-y-6 max-w-4xl">
      <header>
        <div className="label-eyebrow">Authoring</div>
        <h1 className="font-serif text-2xl font-medium text-[var(--ink-50)] mt-1 flex items-center gap-2">
          <Bot className="w-5 h-5 text-indigo-400" /> Agent Studio
        </h1>
        <p className="text-sm text-[var(--ink-300)] mt-1">
          Define an agent, run it in a sandbox with tools mocked, then publish it to the registry. Same
          runtime at every step — what you test is what ships.{" "}
          <Link href="/agents/new" className="text-indigo-300 hover:underline">
            Prefer raw YAML?
          </Link>
        </p>
      </header>

      <ol className="flex items-center gap-2 text-xs">
        {STEPS.map((label, i) => (
          <li key={label} className="flex items-center gap-2">
            <span
              className={`px-2.5 py-1 rounded-full font-medium ${
                i === step
                  ? "bg-indigo-600 text-white"
                  : i < step
                    ? "bg-indigo-500/15 text-indigo-300"
                    : "bg-[var(--surface-2)] text-[var(--ink-500)]"
              }`}
            >
              {i < step ? <Check className="w-3 h-3 inline" /> : i + 1} {label}
            </span>
            {i < STEPS.length - 1 && <ArrowRight className="w-3 h-3 text-[var(--ink-500)]" />}
          </li>
        ))}
      </ol>

      {error && (
        <div className="rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300 font-mono">
          {error}
        </div>
      )}

      {step === 0 && (
        <section className="panel p-5 space-y-4">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="label-eyebrow">Start from</span>
            {TEMPLATES.map((t) => (
              <button
                key={t.label}
                type="button"
                onClick={() => {
                  setTitle(t.title);
                  setName(slug(t.title));
                  setSystemPrompt(t.prompt);
                  setTools(t.tools);
                  setCases([t.check]);
                }}
                className="btn-secondary !py-1 !px-2 !text-[11px]"
              >
                {t.label}
              </button>
            ))}
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div>
              <label htmlFor="ag-title" className="label-eyebrow">
                Title
              </label>
              <input
                id="ag-title"
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                placeholder="Release Notes Writer"
                className={field}
              />
            </div>
            <div>
              <label htmlFor="ag-name" className="label-eyebrow">
                Registry name
              </label>
              <input
                id="ag-name"
                value={agentName}
                onChange={(e) => setName(slug(e.target.value))}
                placeholder="release-notes-writer"
                className={`${field} font-mono`}
              />
            </div>
          </div>

          <div>
            <label htmlFor="ag-desc" className="label-eyebrow">
              Description
            </label>
            <input
              id="ag-desc"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="Turns a merged diff into customer-facing release notes."
              className={field}
            />
          </div>

          <div>
            <label htmlFor="ag-prompt" className="label-eyebrow">
              System prompt
            </label>
            <textarea
              id="ag-prompt"
              value={systemPrompt}
              onChange={(e) => setSystemPrompt(e.target.value)}
              rows={8}
              placeholder="You are a release manager. Given a diff, write release notes that…"
              className={`${field} font-mono text-[13px]`}
            />
          </div>

          <div className="grid grid-cols-3 gap-3">
            <ModelPicker
              provider={provider}
              model={model}
              onChange={(next) => {
                setProvider(next.provider);
                setModel(next.model);
              }}
              className="col-span-2"
            />
            <div>
              <label htmlFor="ag-turns" className="label-eyebrow">
                Max turns
              </label>
              <input
                id="ag-turns"
                type="number"
                min={1}
                max={20}
                value={maxTurns}
                onChange={(e) => setMaxTurns(Number(e.target.value))}
                className={field}
              />
            </div>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div>
              <label htmlFor="ag-tools" className="label-eyebrow">
                Tools
              </label>
              <input
                id="ag-tools"
                value={tools}
                onChange={(e) => setTools(e.target.value)}
                placeholder="scm_list_files, scm_read_file"
                className={`${field} font-mono`}
              />
              <p className="text-[11px] text-[var(--ink-500)] mt-1">
                Built-in tool names the agent may call. Anything outside this list is refused before the
                sandbox even sees it.
              </p>
            </div>
            <div>
              <label htmlFor="ag-skills" className="label-eyebrow">
                Skills
              </label>
              <input
                id="ag-skills"
                value={skills}
                onChange={(e) => setSkills(e.target.value)}
                placeholder="code-review, changelog-writing"
                className={`${field} font-mono`}
              />
              <p className="text-[11px] text-[var(--ink-500)] mt-1">Registry Skills to compose in.</p>
            </div>
          </div>

          {blocking.length > 0 && (
            <ul className="text-xs text-red-300 space-y-1">
              {blocking.map((i) => (
                <li key={i.message}>{i.message}</li>
              ))}
            </ul>
          )}

          <div className="flex justify-end">
            <button type="button" onClick={startTest} disabled={!definable || busy} className="btn-primary !py-1.5 !px-3 !text-xs disabled:opacity-50">
              <FlaskConical className="w-3.5 h-3.5" /> {busy ? "Creating sandbox…" : "Test in sandbox"}
            </button>
          </div>
        </section>
      )}

      {step === 1 && (
        <section className="space-y-3">
          <div className="panel px-4 py-3 flex items-center justify-between gap-3">
            <div className="text-xs text-[var(--ink-300)]">
              Running <span className="font-mono text-[var(--ink-100)]">{agentName}</span> on{" "}
              <span className="font-mono text-[var(--ink-100)]">{provider}/{model}</span> with tools mocked.
              <Link href={`/sandboxes/${sandboxId}`} className="ml-2 text-indigo-300 hover:underline font-mono">
                {sandboxId.slice(0, 8)}
              </Link>
            </div>
            {stale && (
              <button type="button" onClick={startTest} disabled={busy} className="btn-secondary !py-1 !px-2 !text-[11px]">
                Definition changed — new sandbox
              </button>
            )}
          </div>

          {sandboxId && <EvalPanel sandboxId={sandboxId} cases={cases} onCasesChange={setCases} />}
          {sandboxId && (
            <ModelCompare
              sandboxId={sandboxId}
              agentName={agentName}
              draft={manifest}
              cases={cases}
              provider={provider}
              model={model}
            />
          )}
          {sandboxId && <SandboxConsole sandboxId={sandboxId} live={false} />}

          <div className="flex justify-between">
            <button type="button" onClick={() => setStep(0)} className="btn-secondary !py-1.5 !px-3 !text-xs">
              <ArrowLeft className="w-3.5 h-3.5" /> Edit definition
            </button>
            <button type="button" onClick={() => setStep(2)} className="btn-primary !py-1.5 !px-3 !text-xs">
              It behaves — publish <ArrowRight className="w-3.5 h-3.5" />
            </button>
          </div>
        </section>
      )}

      {step === 2 && (
        <section className="panel p-5 space-y-4">
          {published ? (
            <div className="space-y-3">
              <p className="text-sm text-[var(--ink-100)]">
                <span className="font-mono">{published}</span> is in the registry. It is runnable now — no
                restart, no redeploy.
              </p>
              <div className="flex gap-2">
                <button type="button" onClick={() => router.push("/agents")} className="btn-primary !py-1.5 !px-3 !text-xs">
                  Back to agents
                </button>
                <Link href={`/sandboxes/${sandboxId}`} className="btn-secondary !py-1.5 !px-3 !text-xs">
                  Open the sandbox
                </Link>
              </div>
            </div>
          ) : (
            <>
              <p className="text-sm text-[var(--ink-300)]">
                Publishing puts this definition in the catalog under{" "}
                <span className="font-mono text-[var(--ink-100)]">{agentName}</span>, where pipelines and new
                sandboxes can pin it.
              </p>
              <pre className="text-[11px] font-mono text-[var(--ink-300)] bg-[var(--surface-2)] rounded-md p-3 overflow-x-auto max-h-72">
                {JSON.stringify(manifest, null, 2)}
              </pre>

              {conflict && (
                <div className="rounded-md border border-amber-500/30 bg-amber-500/10 p-3 space-y-2">
                  <p className="text-xs text-amber-200">
                    <span className="font-mono">{agentName}</span> already exists. Publishing again versions
                    it — this is what would change:
                  </p>
                  <dl className="text-[11px] font-mono space-y-1">
                    {changedFields(conflict, manifest.spec as Record<string, unknown>).map((d) => (
                      <div key={d.key}>
                        <dt className="text-[var(--ink-500)]">{d.key}</dt>
                        <dd className="text-red-300 truncate">− {d.before}</dd>
                        <dd className="text-emerald-300 truncate">+ {d.after}</dd>
                      </div>
                    ))}
                  </dl>
                </div>
              )}

              <div className="flex justify-between">
                <button type="button" onClick={() => setStep(1)} className="btn-secondary !py-1.5 !px-3 !text-xs">
                  <ArrowLeft className="w-3.5 h-3.5" /> Back to testing
                </button>
                <button
                  type="button"
                  onClick={() => publish(Boolean(conflict))}
                  disabled={busy}
                  className="btn-primary !py-1.5 !px-3 !text-xs disabled:opacity-50"
                >
                  <Rocket className="w-3.5 h-3.5" />{" "}
                  {busy ? "Publishing…" : conflict ? "Publish as a new version" : "Publish to registry"}
                </button>
              </div>
            </>
          )}
        </section>
      )}
    </div>
  );
}
