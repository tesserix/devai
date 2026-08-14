"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Boxes, Plus } from "lucide-react";
import { SandboxCreateDialog } from "@/components/sandbox-create-dialog";

type Sandbox = {
  id: string;
  owner: string;
  status: string;
  created_at: string;
  expires_at: string;
  spec: {
    agent: { name: string; version: string };
    model: { provider: string; model: string };
    workspace?: boolean;
    ide?: boolean;
    repo?: { url: string; ref: string } | null;
  };
};

const DOT: Record<string, string> = {
  ready: "dot-ok",
  provisioning: "dot-running",
  pending: "dot-running",
  destroying: "dot-muted",
  destroyed: "dot-muted",
  failed: "dot-error",
};

export default function SandboxesPage() {
  const [sandboxes, setSandboxes] = useState<Sandbox[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const router = useRouter();

  useEffect(() => {
    fetch("/api/sandboxes", { credentials: "include" })
      .then(async (r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}: ${(await r.text()).slice(0, 200)}`);
        return r.json();
      })
      .then((data: Sandbox[]) => setSandboxes(data))
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false));
  }, []);

  return (
    <div className="p-7 space-y-6">
      <header className="flex items-start justify-between gap-3">
        <div>
          <div className="label-eyebrow">Evaluation</div>
          <h1 className="font-serif text-2xl font-medium text-[var(--ink-50)] mt-1 flex items-center gap-2">
            <Boxes className="w-5 h-5 text-indigo-400" /> Sandboxes
          </h1>
          <p className="text-sm text-[var(--ink-300)] mt-1">
            A pinned agent, model and runtime you can talk to and measure. Open one to try the agent,
            read its traces, and see the diff or the app it produced.
          </p>
        </div>
        <button type="button" onClick={() => setCreating(true)} className="btn-primary !py-1 !px-3 !text-xs">
          <Plus className="w-3 h-3" /> New sandbox
        </button>
      </header>

      <SandboxCreateDialog
        open={creating}
        onClose={() => setCreating(false)}
        onCreated={(id) => router.push(`/sandboxes/${encodeURIComponent(id)}`)}
      />

      {error && (
        <div className="rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300 font-mono">
          {error}
        </div>
      )}

      {loading ? (
        <div className="text-sm text-[var(--ink-500)]">Loading…</div>
      ) : sandboxes.length === 0 ? (
        <div className="text-sm text-[var(--ink-500)]">
          No sandboxes yet. Create one to try a published agent end to end.
        </div>
      ) : (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
          {sandboxes.map((s) => (
            <Link
              key={s.id}
              href={`/sandboxes/${encodeURIComponent(s.id)}`}
              className="panel p-4 block hover:border-[var(--surface-border-strong)] transition-colors"
            >
              <div className="flex items-baseline justify-between gap-2">
                <h2 className="text-sm font-mono font-semibold text-[var(--ink-50)]">{s.id}</h2>
                <span className="inline-flex items-center gap-1.5 text-[11px] text-[var(--ink-300)]">
                  <span className={DOT[s.status] ?? "dot-muted"} /> {s.status}
                </span>
              </div>
              <dl className="mt-3 grid grid-cols-2 gap-x-3 gap-y-1 text-xs">
                <dt className="label-eyebrow">Agent</dt>
                <dd className="font-mono text-[var(--ink-100)]">
                  {s.spec.agent.name}@{s.spec.agent.version}
                </dd>
                <dt className="label-eyebrow">Model</dt>
                <dd className="font-mono text-[var(--ink-100)]">
                  {s.spec.model.provider}/{s.spec.model.model}
                </dd>
                {s.spec.repo && (
                  <>
                    <dt className="label-eyebrow">Repo</dt>
                    <dd className="font-mono text-[var(--ink-100)] truncate">
                      {s.spec.repo.url}@{s.spec.repo.ref}
                    </dd>
                  </>
                )}
                <dt className="label-eyebrow">Expires</dt>
                <dd className="text-[var(--ink-100)]">{new Date(s.expires_at).toLocaleString()}</dd>
              </dl>
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}
