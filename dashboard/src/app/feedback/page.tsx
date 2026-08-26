"use client";

import { FormEvent, useState } from "react";
import { api } from "@/lib/api";

export default function FeedbackPage() {
  const [type, setType] = useState<"story" | "bug" | "task">("story");
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [result, setResult] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setPending(true);
    setResult(null);
    try {
      const issue = await api.submitFeedback({ type, title, description });
      setResult(`Thanks — GitHub issue #${issue.issue_number} was created for the DevAI team.`);
      setTitle("");
      setDescription("");
    } catch (error) {
      setResult(error instanceof Error ? error.message : "Feedback could not be submitted.");
    } finally {
      setPending(false);
    }
  }

  return (
    <main className="mx-auto max-w-2xl px-6 py-10">
      <h1 className="text-2xl font-semibold text-[var(--ink-strong)]">Share feedback</h1>
      <p className="mt-2 text-sm text-[var(--ink-muted)]">
        Tell us what would make DevAI better. We route every submission to the product board.
      </p>
      <form onSubmit={submit} className="mt-8 space-y-5 rounded-xl border border-[var(--border)] bg-[var(--surface)] p-6">
        <label className="block text-sm font-medium">What kind of feedback is this?
          <select value={type} onChange={(e) => setType(e.target.value as typeof type)} className="mt-2 w-full rounded-lg border border-[var(--border)] bg-[var(--surface)] p-2.5">
            <option value="story">User story / requirement</option>
            <option value="bug">Bug report</option>
            <option value="task">Task / other improvement</option>
          </select>
        </label>
        <label className="block text-sm font-medium">Title
          <input value={title} onChange={(e) => setTitle(e.target.value)} required maxLength={200} className="mt-2 w-full rounded-lg border border-[var(--border)] bg-[var(--surface)] p-2.5" />
        </label>
        <label className="block text-sm font-medium">Details
          <textarea value={description} onChange={(e) => setDescription(e.target.value)} required maxLength={10000} rows={7} className="mt-2 w-full rounded-lg border border-[var(--border)] bg-[var(--surface)] p-2.5" />
        </label>
        <button disabled={pending} className="rounded-lg bg-indigo-600 px-4 py-2.5 text-sm font-medium text-white disabled:opacity-50">{pending ? "Submitting…" : "Submit feedback"}</button>
        {result && <p className="text-sm text-[var(--ink-muted)]">{result}</p>}
      </form>
    </main>
  );
}
