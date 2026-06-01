"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Check, Loader2, Sparkles } from "lucide-react";

import { api } from "@/lib/api";

const CATEGORIES = ["coding", "review", "planning", "research", "ops", "data", "general"];

/**
 * Author a Skill — a named, reusable capability published to the shared
 * registry so any agent can compose it (it shows up in the Create-Agent
 * Skills picker). Saving POSTs to /api/authoring/skills, which persists it
 * and publishes the Skill envelope to the catalog.
 */
export default function NewSkillPage() {
  const router = useRouter();

  const [name, setName] = useState("");
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [category, setCategory] = useState("coding");
  const [tags, setTags] = useState("");
  const [repository, setRepository] = useState("");

  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);

  const nameValid = /^[a-z][a-z0-9-]{1,63}$/.test(name);

  async function save() {
    setError(null);
    setDone(null);
    if (!nameValid) {
      setError("Name must be lowercase kebab-case (e.g. code-review), 2–64 chars.");
      return;
    }
    setSaving(true);
    try {
      const res = await api.createSkill({
        name,
        title: title || name,
        description,
        category,
        tags: tags
          .split(",")
          .map((t) => t.trim())
          .filter(Boolean),
        repository: repository || undefined,
      });
      setDone(res.name);
      setTimeout(() => router.push("/skills"), 900);
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
          <Sparkles className="w-5 h-5 text-indigo-400" /> Author Skill
        </h1>
        <p className="text-sm text-[var(--ink-300)] mt-1">
          Describe a reusable capability. Once published it appears in the registry and in the
          Create-Agent Skills picker for anyone to compose.
        </p>
      </header>

      {error && (
        <div className="rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300 font-mono">
          {error}
        </div>
      )}
      {done && (
        <div className="rounded-md border border-emerald-500/30 bg-emerald-500/10 px-3 py-2 text-sm text-emerald-300 flex items-center gap-2">
          <Check className="w-4 h-4" /> Published skill <span className="font-mono">{done}</span> — redirecting…
        </div>
      )}

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <Field label="Name (kebab-case)">
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="code-review"
            className={inputCls + (name && !nameValid ? " border-red-500/50" : "")}
          />
        </Field>
        <Field label="Title">
          <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Code Review" className={inputCls} />
        </Field>
        <Field label="Category">
          <select value={category} onChange={(e) => setCategory(e.target.value)} className={inputCls}>
            {CATEGORIES.map((c) => (
              <option key={c} value={c}>{c}</option>
            ))}
          </select>
        </Field>
        <Field label="Tags (comma-separated)">
          <input value={tags} onChange={(e) => setTags(e.target.value)} placeholder="python, security" className={inputCls} />
        </Field>
      </div>

      <Field label="Description">
        <textarea
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          rows={4}
          placeholder="What this skill does and when an agent should use it."
          className={inputCls}
        />
      </Field>

      <Field label="Source repository (optional)">
        <input
          value={repository}
          onChange={(e) => setRepository(e.target.value)}
          placeholder="github.com/tesserix/skills"
          className={inputCls}
        />
      </Field>

      <div className="flex items-center gap-3">
        <button
          type="button"
          onClick={save}
          disabled={saving}
          className="inline-flex items-center gap-2 px-4 py-2 rounded-md bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 text-white text-sm font-medium"
        >
          {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Sparkles className="w-4 h-4" />}
          Publish skill
        </button>
        <button
          type="button"
          onClick={() => router.push("/skills")}
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
