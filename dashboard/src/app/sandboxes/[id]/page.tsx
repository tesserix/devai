"use client";

import { use, useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { ArrowLeft, ExternalLink } from "lucide-react";

type Snapshot = {
  captured_at?: string;
  diff?: string;
  status?: string;
  entries?: string[];
  errors?: Record<string, string>;
};

type Sandbox = {
  id: string;
  owner: string;
  status: string;
  created_at: string;
  expires_at: string;
  detail?: { snapshot?: Snapshot; workspace?: { endpoint: string } };
  spec: {
    agent: { name: string; version: string };
    model: { provider: string; model: string };
    ide?: boolean;
    repo?: { url: string; ref: string } | null;
  };
};

export default function SandboxPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const [sandbox, setSandbox] = useState<Sandbox | null>(null);
  const [ports, setPorts] = useState<number[]>([]);
  const [port, setPort] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  const base = `/api/sandboxes/${encodeURIComponent(id)}`;

  const load = useCallback(async () => {
    const res = await fetch(base, { credentials: "include" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return (await res.json()) as Sandbox;
  }, [base]);

  useEffect(() => {
    load()
      .then(setSandbox)
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
    // A destroyed sandbox has no ports; a soft failure here is not an error.
    fetch(`${base}/preview`, { credentials: "include" })
      .then((r) => (r.ok ? r.json() : { ports: [] }))
      .then((d: { ports: number[] }) => {
        setPorts(d.ports);
        setPort(d.ports[0] ?? null);
      })
      .catch(() => setPorts([]));
  }, [base, load]);

  const snapshot = sandbox?.detail?.snapshot;
  const live = sandbox?.status === "ready";

  return (
    <div className="p-7 space-y-6">
      <header className="flex items-start justify-between gap-3">
        <div>
          <Link
            href="/sandboxes"
            className="inline-flex items-center gap-1 text-xs text-[var(--ink-500)] hover:text-[var(--ink-300)]"
          >
            <ArrowLeft className="w-3 h-3" /> Sandboxes
          </Link>
          <h1 className="font-serif text-2xl font-medium text-[var(--ink-50)] mt-1 font-mono">{id}</h1>
          {sandbox && (
            <p className="text-sm text-[var(--ink-300)] mt-1">
              {sandbox.spec.agent.name}@{sandbox.spec.agent.version} · {sandbox.spec.model.provider}/
              {sandbox.spec.model.model} · {sandbox.status}
            </p>
          )}
        </div>
        {live && sandbox?.spec.ide && (
          <a
            href={`${base}/ide/`}
            target="_blank"
            rel="noreferrer"
            className="btn-secondary !py-1 !px-2 !text-xs"
          >
            <ExternalLink className="w-3 h-3" /> Open in editor
          </a>
        )}
      </header>

      {error && (
        <div className="rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300 font-mono">
          {error}
        </div>
      )}

      {ports.length > 0 && (
        <section className="panel p-0 overflow-hidden">
          <div className="px-4 py-2 flex items-center gap-3 text-xs border-b border-[var(--surface-border)]">
            <span className="label-eyebrow">What it built</span>
            {ports.map((p) => (
              <button
                key={p}
                type="button"
                onClick={() => setPort(p)}
                className={`font-mono ${p === port ? "text-[var(--ink-50)]" : "text-[var(--ink-500)]"}`}
              >
                :{p}
              </button>
            ))}
          </div>
          {/*
            The framed app is whatever the agent wrote. allow-scripts without
            allow-same-origin keeps it running but unable to reach this document
            — see the run preview for the same reasoning.
          */}
          {port !== null && (
            <iframe
              src={`${base}/preview/${port}/`}
              className="w-full h-[420px]"
              style={{ border: "none", background: "white" }}
              sandbox="allow-scripts allow-forms allow-popups"
              referrerPolicy="no-referrer"
              title={`Preview of sandbox ${id} on port ${port}`}
            />
          )}
        </section>
      )}

      {snapshot?.diff && (
        <section className="panel p-4">
          <div className="label-eyebrow">Diff the agent produced</div>
          <pre className="mt-2 text-xs font-mono text-[var(--ink-100)] overflow-x-auto whitespace-pre">
            {snapshot.diff}
          </pre>
        </section>
      )}

      {snapshot?.entries && snapshot.entries.length > 0 && (
        <section className="panel p-4">
          <div className="label-eyebrow">Tree it left</div>
          <ul className="mt-2 text-xs font-mono text-[var(--ink-300)] columns-2">
            {snapshot.entries.map((e) => (
              <li key={e}>{e}</li>
            ))}
          </ul>
        </section>
      )}

      {snapshot?.errors && Object.keys(snapshot.errors).length > 0 && (
        <p className="text-xs text-[var(--ink-500)]">
          Part of the snapshot could not be taken: {Object.keys(snapshot.errors).join(", ")}.
        </p>
      )}
    </div>
  );
}
