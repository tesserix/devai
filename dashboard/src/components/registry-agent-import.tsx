"use client";

import { FormEvent, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Box, Download, LockKeyhole, Search } from "lucide-react";

import { api, type AgentImport, type RegistrySearchHit } from "@/lib/api";
import { importedAgentModel, registryAgentReference } from "@/lib/registry-import";

function conformanceMessage(level: string): string {
  if (level === "sandbox_runnable") return "Digest-pinned container: isolated DevAI runtime supported.";
  if (level === "callable") return "Authenticated remote runtime: DevAI evaluates calls but does not host the agent.";
  return `Conformance: ${level || "unknown"}`;
}

export function RegistryAgentImport() {
  const router = useRouter();
  const [projectId, setProjectId] = useState("agent-lab");
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<RegistrySearchHit[]>([]);
  const [imports, setImports] = useState<AgentImport[]>([]);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    if (!projectId.trim()) return;
    let cancelled = false;
    api.listAgentImports(projectId.trim())
      .then((found) => {
        if (!cancelled) setImports(found);
      })
      .catch(() => {
        if (!cancelled) setImports([]);
      });
    return () => {
      cancelled = true;
    };
  }, [projectId]);

  async function search(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!query.trim()) return;
    setBusy("search");
    setError("");
    try {
      const result = await api.searchRegistry(query.trim(), ["Agent"], 20);
      setHits(result.hits);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy("");
    }
  }

  async function importHit(hit: RegistrySearchHit) {
    setBusy(`import:${hit.arn}`);
    setError("");
    try {
      const imported = await api.createAgentImport(
        { project_id: projectId.trim(), registry_ref: registryAgentReference(hit) },
        crypto.randomUUID(),
      );
      setImports((current) => [imported, ...current.filter((item) => item.id !== imported.id)]);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy("");
    }
  }

  async function createSandbox(imported: AgentImport) {
    setBusy(`sandbox:${imported.id}`);
    setError("");
    try {
      const sandbox = await api.createSandbox({
        import_id: imported.id,
        model: importedAgentModel(imported),
        tools: { default_mode: "mock", overrides: {} },
      });
      router.push(`/sandboxes/${encodeURIComponent(sandbox.id)}`);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy("");
    }
  }

  return (
    <section className="panel p-4 space-y-4" aria-labelledby="registry-import-title">
      <div>
        <h2 id="registry-import-title" className="flex items-center gap-2 text-sm font-medium text-[var(--ink-50)]">
          <Download className="h-4 w-4 text-indigo-300" /> Bring your own agent
        </h2>
        <p className="mt-1 text-xs text-[var(--ink-500)]">
          Search by behavior, verify the Registry signature, and freeze every dependency before DevAI runs anything.
        </p>
      </div>

      <form onSubmit={search} className="grid gap-3 md:grid-cols-[180px_1fr_auto]">
        <label className="block">
          <span className="label-eyebrow">DevAI project</span>
          <input
            value={projectId}
            onChange={(event) => setProjectId(event.target.value)}
            pattern="[A-Za-z0-9][A-Za-z0-9._-]*"
            required
            className="mt-1 w-full rounded-md border border-[var(--surface-border)] bg-[var(--surface-2)] px-3 py-2 text-sm font-mono"
          />
        </label>
        <label className="block">
          <span className="label-eyebrow">What should the agent do?</span>
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            required
            maxLength={512}
            placeholder="Triage support incidents using our knowledge base"
            className="mt-1 w-full rounded-md border border-[var(--surface-border)] bg-[var(--surface-2)] px-3 py-2 text-sm"
          />
        </label>
        <button type="submit" disabled={busy === "search"} className="btn-primary self-end disabled:opacity-50">
          <Search className="h-4 w-4" /> {busy === "search" ? "Searching…" : "Search"}
        </button>
      </form>

      {error && <p className="rounded-md bg-red-500/10 px-3 py-2 text-xs font-mono text-red-300">{error}</p>}

      {hits.length > 0 && (
        <div className="space-y-2">
          <div className="label-eyebrow">Semantic matches</div>
          {hits.map((hit) => (
            <article key={`${hit.arn}@${hit.version}`} className="rounded-md border border-[var(--surface-border)] p-3">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <div className="font-mono text-sm text-[var(--ink-50)]">{hit.name}@{hit.version}</div>
                  <p className="mt-1 text-xs text-[var(--ink-300)]">{hit.description}</p>
                  <p className="mt-1 text-[11px] font-mono text-[var(--ink-500)]">{hit.digest}</p>
                </div>
                <button
                  type="button"
                  onClick={() => void importHit(hit)}
                  disabled={!projectId.trim() || busy === `import:${hit.arn}`}
                  className="btn-secondary !py-1 !px-2 !text-xs disabled:opacity-50"
                >
                  <LockKeyhole className="h-3 w-3" /> Verify & import
                </button>
              </div>
            </article>
          ))}
        </div>
      )}

      {imports.length > 0 && (
        <div className="space-y-2">
          <div className="label-eyebrow">Immutable import locks</div>
          {imports.map((imported) => (
            <article key={imported.id} className="rounded-md border border-emerald-500/20 bg-emerald-500/5 p-3">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <div className="font-mono text-sm text-[var(--ink-50)]">
                    {imported.agent.name}@{imported.agent.version}
                  </div>
                  <p className="mt-1 text-xs text-emerald-300">{conformanceMessage(imported.conformance.level)}</p>
                  <p className="mt-1 text-[11px] font-mono text-[var(--ink-500)]">{imported.agent.digest}</p>
                </div>
                <button
                  type="button"
                  onClick={() => void createSandbox(imported)}
                  disabled={busy === `sandbox:${imported.id}`}
                  className="btn-primary !py-1 !px-2 !text-xs disabled:opacity-50"
                >
                  <Box className="h-3 w-3" /> Create sandbox
                </button>
              </div>
              <details className="mt-3 text-xs">
                <summary className="cursor-pointer text-[var(--ink-300)]">
                  Exact dependency lock ({imported.dependency_lock.length})
                </summary>
                <ul className="mt-2 space-y-1 font-mono text-[11px] text-[var(--ink-500)]">
                  {imported.dependency_lock.map((dependency) => (
                    <li key={`${dependency.kind}:${dependency.namespace}:${dependency.name}@${dependency.version}`}>
                      {dependency.kind} {dependency.namespace}/{dependency.name}@{dependency.version} · {dependency.digest}
                    </li>
                  ))}
                </ul>
              </details>
            </article>
          ))}
        </div>
      )}
    </section>
  );
}
