"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Check, Loader2, Wrench } from "lucide-react";

import { api, type CreateMcpServerInput } from "@/lib/api";

type Mode = "remote" | "image";

/**
 * Register a custom tool as an MCP server — either a reachable remote
 * endpoint or a container image the platform runs in the MCP sandbox.
 * Published to the registry so agents can pick it from the MCP Servers
 * picker; once routed through agentgateway it's callable at /mcp/<name>.
 */
export default function NewToolPage() {
  const router = useRouter();

  const [mode, setMode] = useState<Mode>("remote");
  const [name, setName] = useState("");
  const [version, setVersion] = useState("1.0.0");
  const [description, setDescription] = useState("");

  // remote mode
  const [url, setUrl] = useState("");
  const [transport, setTransport] = useState("streamableHttp");
  const [headers, setHeaders] = useState(""); // "Key: value" per line

  // image mode
  const [image, setImage] = useState("");
  const [command, setCommand] = useState("");
  const [args, setArgs] = useState(""); // space/newline separated
  const [env, setEnv] = useState(""); // "KEY=value" per line

  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);

  const nameValid = /^[a-z][a-z0-9-]{1,63}$/.test(name);

  function parseKV(text: string, sep: string): Record<string, string> {
    const out: Record<string, string> = {};
    for (const line of text.split("\n")) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      const i = trimmed.indexOf(sep);
      if (i === -1) continue;
      out[trimmed.slice(0, i).trim()] = trimmed.slice(i + 1).trim();
    }
    return out;
  }

  async function save() {
    setError(null);
    setDone(null);
    if (!nameValid) {
      setError("Name must be lowercase kebab-case (e.g. my-tool), 2–64 chars.");
      return;
    }
    if (mode === "remote" && !url.trim()) {
      setError("A remote MCP endpoint URL is required.");
      return;
    }
    if (mode === "image" && !image.trim()) {
      setError("A container image is required.");
      return;
    }
    setSaving(true);
    try {
      const body: CreateMcpServerInput = { name, version, description };
      if (mode === "remote") {
        body.url = url;
        body.transport = transport;
        const h = parseKV(headers, ":");
        if (Object.keys(h).length) body.headers = h;
      } else {
        body.image = image;
        if (command) body.command = command;
        const a = args.split(/\s+/).filter(Boolean);
        if (a.length) body.args = a;
        const e = parseKV(env, "=");
        if (Object.keys(e).length) body.env = e;
      }
      const res = await api.createMcpServer(body);
      setDone(res.name);
      setTimeout(() => router.push("/tools"), 900);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="p-7 space-y-6 max-w-3xl">
      <header>
        <div className="label-eyebrow">Authoring</div>
        <h1 className="font-serif text-2xl font-medium text-[var(--ink-50)] mt-1 flex items-center gap-2">
          <Wrench className="w-5 h-5 text-indigo-400" /> Register Tool
        </h1>
        <p className="text-sm text-[var(--ink-300)] mt-1">
          Register a custom tool as an MCP server. Published to the registry and selectable from the
          Create-Agent MCP Servers picker.
        </p>
      </header>

      {error && (
        <div className="rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300 font-mono">
          {error}
        </div>
      )}
      {done && (
        <div className="rounded-md border border-emerald-500/30 bg-emerald-500/10 px-3 py-2 text-sm text-emerald-300 flex items-center gap-2">
          <Check className="w-4 h-4" /> Registered tool <span className="font-mono">{done}</span> — redirecting…
        </div>
      )}

      {/* Mode toggle */}
      <div className="inline-flex rounded-md border border-[var(--surface-border)] overflow-hidden">
        {(["remote", "image"] as Mode[]).map((m) => (
          <button
            key={m}
            type="button"
            onClick={() => setMode(m)}
            className={
              "px-4 py-1.5 text-sm transition-colors " +
              (mode === m
                ? "bg-indigo-600 text-white"
                : "bg-[var(--surface-2)] text-[var(--ink-300)] hover:text-[var(--ink-100)]")
            }
          >
            {m === "remote" ? "Remote endpoint" : "Container image"}
          </button>
        ))}
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <Field label="Name (kebab-case)">
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="my-tool"
            className={inputCls + (name && !nameValid ? " border-red-500/50" : "")}
          />
        </Field>
        <Field label="Version">
          <input value={version} onChange={(e) => setVersion(e.target.value)} placeholder="1.0.0" className={inputCls} />
        </Field>
      </div>

      <Field label="Description">
        <input value={description} onChange={(e) => setDescription(e.target.value)} placeholder="What this tool exposes" className={inputCls} />
      </Field>

      {mode === "remote" ? (
        <>
          <div className="grid grid-cols-1 md:grid-cols-[2fr_1fr] gap-4">
            <Field label="MCP endpoint URL">
              <input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://tools.example.com/mcp" className={inputCls} />
            </Field>
            <Field label="Transport">
              <select value={transport} onChange={(e) => setTransport(e.target.value)} className={inputCls}>
                <option value="streamableHttp">streamableHttp</option>
                <option value="sse">sse</option>
              </select>
            </Field>
          </div>
          <Field label="Headers (one per line, Key: value)">
            <textarea value={headers} onChange={(e) => setHeaders(e.target.value)} rows={3} placeholder={"Authorization: Bearer …"} className={inputCls + " font-mono text-xs"} />
          </Field>
        </>
      ) : (
        <>
          <Field label="Container image">
            <input value={image} onChange={(e) => setImage(e.target.value)} placeholder="ghcr.io/org/my-mcp:latest" className={inputCls} />
          </Field>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <Field label="Command (optional)">
              <input value={command} onChange={(e) => setCommand(e.target.value)} placeholder="node" className={inputCls} />
            </Field>
            <Field label="Args (space-separated)">
              <input value={args} onChange={(e) => setArgs(e.target.value)} placeholder="server.js --port 8080" className={inputCls} />
            </Field>
          </div>
          <Field label="Env (one per line, KEY=value)">
            <textarea value={env} onChange={(e) => setEnv(e.target.value)} rows={3} placeholder={"LOG_LEVEL=info"} className={inputCls + " font-mono text-xs"} />
          </Field>
        </>
      )}

      <div className="flex items-center gap-3">
        <button
          type="button"
          onClick={save}
          disabled={saving}
          className="inline-flex items-center gap-2 px-4 py-2 rounded-md bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 text-white text-sm font-medium"
        >
          {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Wrench className="w-4 h-4" />}
          Register tool
        </button>
        <button
          type="button"
          onClick={() => router.push("/tools")}
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
