"use client";

import { FormEvent, useEffect, useState } from "react";
import { api, type FeedbackThread } from "@/lib/api";
import { feedbackInboxTitle, feedbackStatusLabel } from "@/lib/feedback";

function formatDate(value: string): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(date);
}

function statusClasses(status: FeedbackThread["status"]): string {
  return status === "open"
    ? "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300"
    : "bg-slate-500/10 text-slate-600 dark:text-slate-300";
}

export default function FeedbackPage() {
  const [type, setType] = useState<"story" | "bug" | "task">("story");
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [result, setResult] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [threads, setThreads] = useState<FeedbackThread[]>([]);
  const [selected, setSelected] = useState<FeedbackThread | null>(null);
  const [canManage, setCanManage] = useState(false);
  const [inboxPending, setInboxPending] = useState(true);
  const [detailPending, setDetailPending] = useState(false);
  const [reply, setReply] = useState("");
  const [replyPending, setReplyPending] = useState(false);
  const [inboxError, setInboxError] = useState<string | null>(null);

  async function openThread(threadId: string) {
    setDetailPending(true);
    setInboxError(null);
    try {
      setSelected(await api.getFeedback(threadId));
    } catch (error) {
      setInboxError(error instanceof Error ? error.message : "Feedback could not be opened.");
    } finally {
      setDetailPending(false);
    }
  }

  async function refreshInbox(preferredId?: string) {
    setInboxPending(true);
    setInboxError(null);
    try {
      const inbox = await api.listFeedback();
      setThreads(inbox.threads);
      setCanManage(inbox.can_manage);
      const targetId = preferredId ?? selected?.id ?? inbox.threads[0]?.id;
      if (targetId && inbox.threads.some((thread) => thread.id === targetId)) {
        await openThread(targetId);
      } else {
        setSelected(null);
      }
    } catch (error) {
      setInboxError(error instanceof Error ? error.message : "Feedback could not be loaded.");
    } finally {
      setInboxPending(false);
    }
  }

  useEffect(() => {
    void refreshInbox();
    // The inbox is loaded once; mutations refresh it explicitly.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setPending(true);
    setResult(null);
    try {
      const thread = await api.submitFeedback({ type, title, description });
      setResult(`Thanks — feedback #${thread.issue_number} is now in the support inbox.`);
      setTitle("");
      setDescription("");
      await refreshInbox(thread.id);
    } catch (error) {
      setResult(error instanceof Error ? error.message : "Feedback could not be submitted.");
    } finally {
      setPending(false);
    }
  }

  async function sendReply(event: FormEvent) {
    event.preventDefault();
    if (!selected || !reply.trim()) return;
    setReplyPending(true);
    setInboxError(null);
    try {
      await api.replyToFeedback(selected.id, reply.trim());
      setReply("");
      await refreshInbox(selected.id);
    } catch (error) {
      setInboxError(error instanceof Error ? error.message : "Your reply could not be sent.");
    } finally {
      setReplyPending(false);
    }
  }

  async function changeStatus() {
    if (!selected) return;
    setDetailPending(true);
    setInboxError(null);
    try {
      await api.setFeedbackStatus(selected.id, selected.status === "open" ? "closed" : "open");
      await refreshInbox(selected.id);
    } catch (error) {
      setInboxError(error instanceof Error ? error.message : "The status could not be changed.");
    } finally {
      setDetailPending(false);
    }
  }

  return (
    <main className="mx-auto max-w-6xl px-6 py-10">
      <h1 className="text-2xl font-semibold text-[var(--ink-strong)]">Feedback &amp; support</h1>
      <p className="mt-2 max-w-3xl text-sm text-[var(--ink-muted)]">
        Share a requirement, report a bug, or request an improvement. Keep the conversation here and track it until the DevAI team resolves it.
      </p>

      {inboxError && (
        <div role="alert" className="mt-6 rounded-lg border border-red-500/30 bg-red-500/10 px-4 py-3 text-sm text-red-700 dark:text-red-300">
          {inboxError}
        </div>
      )}

      <div className="mt-8 grid gap-6 lg:grid-cols-[360px_minmax(0,1fr)]">
        <div className="space-y-6">
          <form onSubmit={submit} className="space-y-5 rounded-xl border border-[var(--border)] bg-[var(--surface)] p-5">
            <div>
              <h2 className="font-semibold text-[var(--ink-strong)]">Create feedback</h2>
              <p className="mt-1 text-xs text-[var(--ink-muted)]">You can add follow-up details after submitting.</p>
            </div>
            <label className="block text-sm font-medium">Type
              <select value={type} onChange={(event) => setType(event.target.value as typeof type)} className="mt-2 w-full rounded-lg border border-[var(--border)] bg-[var(--surface)] p-2.5">
                <option value="story">User story / requirement</option>
                <option value="bug">Bug report</option>
                <option value="task">Task / other improvement</option>
              </select>
            </label>
            <label className="block text-sm font-medium">Title
              <input value={title} onChange={(event) => setTitle(event.target.value)} required maxLength={200} className="mt-2 w-full rounded-lg border border-[var(--border)] bg-[var(--surface)] p-2.5" />
            </label>
            <label className="block text-sm font-medium">Details
              <textarea value={description} onChange={(event) => setDescription(event.target.value)} required maxLength={10000} rows={5} className="mt-2 w-full rounded-lg border border-[var(--border)] bg-[var(--surface)] p-2.5" />
            </label>
            <button disabled={pending} className="w-full rounded-lg bg-indigo-600 px-4 py-2.5 text-sm font-medium text-white disabled:opacity-50">
              {pending ? "Submitting…" : "Submit feedback"}
            </button>
            {result && <p className="text-sm text-[var(--ink-muted)]">{result}</p>}
          </form>

          <section className="overflow-hidden rounded-xl border border-[var(--border)] bg-[var(--surface)]">
            <div className="border-b border-[var(--border)] px-5 py-4">
              <h2 className="font-semibold text-[var(--ink-strong)]">{feedbackInboxTitle(canManage)}</h2>
              <p className="mt-1 text-xs text-[var(--ink-muted)]">
                {canManage ? "Reply to users and resolve completed requests." : "Open a request to add details or read a response."}
              </p>
            </div>
            {inboxPending && threads.length === 0 ? (
              <p className="px-5 py-6 text-sm text-[var(--ink-muted)]">Loading feedback…</p>
            ) : threads.length === 0 ? (
              <p className="px-5 py-6 text-sm text-[var(--ink-muted)]">No feedback has been submitted yet.</p>
            ) : (
              <div className="max-h-[440px] divide-y divide-[var(--border)] overflow-y-auto">
                {threads.map((thread) => (
                  <button
                    key={thread.id}
                    type="button"
                    onClick={() => void openThread(thread.id)}
                    className={`w-full px-5 py-4 text-left transition hover:bg-black/[0.025] dark:hover:bg-white/[0.04] ${selected?.id === thread.id ? "bg-indigo-500/[0.07]" : ""}`}
                  >
                    <div className="flex items-center justify-between gap-3">
                      <span className="truncate text-sm font-medium text-[var(--ink-strong)]">{thread.title}</span>
                      <span className={`shrink-0 rounded-full px-2 py-0.5 text-[11px] font-medium ${statusClasses(thread.status)}`}>
                        {feedbackStatusLabel(thread.status)}
                      </span>
                    </div>
                    <div className="mt-2 flex items-center justify-between gap-3 text-xs text-[var(--ink-muted)]">
                      <span>#{thread.issue_number} · {thread.type}</span>
                      <span>{formatDate(thread.updated_at)}</span>
                    </div>
                  </button>
                ))}
              </div>
            )}
          </section>
        </div>

        <section className="min-h-[560px] rounded-xl border border-[var(--border)] bg-[var(--surface)]">
          {!selected ? (
            <div className="flex min-h-[560px] items-center justify-center p-8 text-center text-sm text-[var(--ink-muted)]">
              Select a feedback request to view its conversation.
            </div>
          ) : (
            <div className={detailPending ? "opacity-70" : ""}>
              <header className="border-b border-[var(--border)] px-6 py-5">
                <div className="flex flex-wrap items-start justify-between gap-4">
                  <div>
                    <div className="flex flex-wrap items-center gap-2 text-xs text-[var(--ink-muted)]">
                      <span className={`rounded-full px-2 py-0.5 font-medium ${statusClasses(selected.status)}`}>{feedbackStatusLabel(selected.status)}</span>
                      <span>{selected.type}</span>
                      <a href={selected.issue_url} target="_blank" rel="noreferrer" className="text-indigo-600 hover:underline">GitHub #{selected.issue_number}</a>
                    </div>
                    <h2 className="mt-3 text-xl font-semibold text-[var(--ink-strong)]">{selected.title}</h2>
                    {canManage && selected.submitter && <p className="mt-1 text-xs text-[var(--ink-muted)]">Submitted by {selected.submitter}</p>}
                  </div>
                  {selected.can_manage && (
                    <button type="button" onClick={() => void changeStatus()} disabled={detailPending} className="rounded-lg border border-[var(--border)] px-3 py-2 text-xs font-medium disabled:opacity-50">
                      {selected.status === "open" ? "Mark resolved" : "Reopen thread"}
                    </button>
                  )}
                </div>
              </header>

              <div className="space-y-4 px-6 py-6">
                <article className="rounded-xl bg-indigo-500/[0.07] p-4">
                  <div className="flex items-center justify-between gap-3 text-xs text-[var(--ink-muted)]">
                    <span className="font-medium text-[var(--ink-strong)]">Initial request</span>
                    <time>{formatDate(selected.created_at)}</time>
                  </div>
                  <p className="mt-3 whitespace-pre-wrap text-sm leading-6 text-[var(--ink-strong)]">{selected.description}</p>
                </article>

                {(selected.replies ?? []).map((message) => (
                  <article key={message.id} className={`rounded-xl border p-4 ${message.author_role === "support" ? "border-emerald-500/20 bg-emerald-500/[0.06]" : "border-[var(--border)]"}`}>
                    <div className="flex items-center justify-between gap-3 text-xs text-[var(--ink-muted)]">
                      <span className="font-medium text-[var(--ink-strong)]">{message.author}{message.author_role === "support" ? " · DevAI support" : ""}</span>
                      <time>{formatDate(message.created_at)}</time>
                    </div>
                    <p className="mt-3 whitespace-pre-wrap text-sm leading-6 text-[var(--ink-strong)]">{message.body}</p>
                  </article>
                ))}

                {(selected.replies ?? []).length === 0 && (
                  <p className="py-3 text-center text-xs text-[var(--ink-muted)]">No replies yet. Add any extra context below.</p>
                )}
              </div>

              <div className="border-t border-[var(--border)] px-6 py-5">
                {selected.can_reply ? (
                  <form onSubmit={sendReply}>
                    <label className="text-sm font-medium">Add a reply
                      <textarea value={reply} onChange={(event) => setReply(event.target.value)} required maxLength={10000} rows={4} placeholder={canManage ? "Respond to this user…" : "Share another detail, result, or question…"} className="mt-2 w-full rounded-lg border border-[var(--border)] bg-[var(--surface)] p-3" />
                    </label>
                    <div className="mt-3 flex justify-end">
                      <button disabled={replyPending || !reply.trim()} className="rounded-lg bg-indigo-600 px-4 py-2.5 text-sm font-medium text-white disabled:opacity-50">
                        {replyPending ? "Sending…" : "Send reply"}
                      </button>
                    </div>
                  </form>
                ) : (
                  <div className="rounded-lg bg-slate-500/[0.08] px-4 py-3 text-sm text-[var(--ink-muted)]">
                    This request has been resolved. A support engineer can reopen it if more work is needed.
                  </div>
                )}
              </div>
            </div>
          )}
        </section>
      </div>
    </main>
  );
}
