"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { clsx } from "clsx";

interface Message {
  role: "user" | "assistant";
  content: string;
  timestamp: Date;
}

interface ChatPanelProps {
  runId?: string;
  repo?: string;
  stage?: string;
}

export function ChatPanel({ runId, repo, stage }: ChatPanelProps) {
  const [messages, setMessages] = useState<Message[]>([
    {
      role: "assistant",
      content:
        "Hi! I'm the DevAI assistant. I can query pipeline runs, agent memory, security findings, SRE incidents, source code, and more.\n\nYou can also **inject additional requirements** into a running pipeline — just tell me what to add and I'll send it to the Supervisor agent.",
      timestamp: new Date(),
    },
  ]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  const sessionId = useRef(`chat-${Date.now()}`);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages]);

  const send = useCallback(async () => {
    if (!input.trim() || loading) return;
    let userMsg = input.trim();
    setInput("");

    // If we have an active run, prepend context so the chat agent knows
    if (runId) {
      userMsg = `[Active pipeline: run_id=${runId}, repo=${repo || "unknown"}, stage=${stage || "unknown"}]\n\n${userMsg}`;
    }

    setMessages((prev) => [...prev, { role: "user", content: input.trim(), timestamp: new Date() }]);
    setLoading(true);

    try {
      const res = await fetch("/chat/api/message", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ message: userMsg, session_id: sessionId.current }),
      });

      if (!res.ok) {
        throw new Error(`Server error (${res.status})`);
      }
      const data = await res.json();
      setMessages((prev) => [
        ...prev,
        { role: "assistant", content: data.response || "No response from the model — check server logs.", timestamp: new Date() },
      ]);
    } catch {
      setMessages((prev) => [
        ...prev,
        { role: "assistant", content: "Failed to connect to the chat API.", timestamp: new Date() },
      ]);
    } finally {
      setLoading(false);
    }
  }, [input, loading, runId, repo, stage]);

  return (
    <div className="flex flex-col h-full bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 overflow-hidden">
      {/* Pipeline context banner */}
      {runId && (
        <div className="px-4 py-2 bg-indigo-50 dark:bg-indigo-950 border-b border-indigo-100 dark:border-indigo-900 flex items-center gap-2">
          <span className={clsx(
            "w-2 h-2 rounded-full shrink-0",
            stage === "deployed" ? "bg-green-500" : stage?.includes("fail") ? "bg-red-500" : "bg-indigo-500 animate-pulse"
          )} />
          <span className="text-xs text-indigo-700 dark:text-indigo-300 font-medium truncate">
            {repo} &mdash; {stage?.replace(/_/g, " ")}
          </span>
          <span className="text-xs text-indigo-400 dark:text-indigo-500 font-mono ml-auto shrink-0">
            {runId.slice(0, 10)}
          </span>
        </div>
      )}

      {/* Messages */}
      <div ref={scrollRef} className="flex-1 overflow-y-auto p-4 space-y-3">
        {messages.map((msg, i) => (
          <div key={i} className={clsx("flex", msg.role === "user" ? "justify-end" : "justify-start")}>
            <div
              className={clsx(
                "max-w-[80%] rounded-xl px-4 py-3 text-sm",
                msg.role === "user"
                  ? "bg-indigo-600 text-white rounded-br-sm"
                  : "bg-gray-50 dark:bg-gray-700 text-gray-800 dark:text-gray-200 border border-gray-200 dark:border-gray-600 rounded-bl-sm"
              )}
            >
              {msg.role === "assistant" ? (
                <div
                  className="prose dark:prose-invert prose-sm max-w-none prose-pre:bg-gray-100 dark:prose-pre:bg-gray-800 prose-code:text-indigo-700 dark:prose-code:text-indigo-400"
                  dangerouslySetInnerHTML={{ __html: renderMarkdown(msg.content) }}
                />
              ) : (
                <p className="whitespace-pre-wrap">{msg.content}</p>
              )}
              <p className="text-xs mt-1.5 opacity-50">
                {msg.timestamp.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
              </p>
            </div>
          </div>
        ))}
        {loading && (
          <div className="flex justify-start">
            <div className="bg-gray-50 dark:bg-gray-700 border border-gray-200 dark:border-gray-600 rounded-xl rounded-bl-sm px-4 py-3">
              <div className="flex items-center gap-1.5">
                <span className="w-1.5 h-1.5 bg-indigo-500 rounded-full animate-bounce" />
                <span className="w-1.5 h-1.5 bg-indigo-500 rounded-full animate-bounce [animation-delay:0.15s]" />
                <span className="w-1.5 h-1.5 bg-indigo-500 rounded-full animate-bounce [animation-delay:0.3s]" />
              </div>
            </div>
          </div>
        )}
      </div>

      {/* Input */}
      <div className="border-t border-gray-200 dark:border-gray-700 p-3 bg-white dark:bg-gray-800">
        <div className="flex gap-2">
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && !e.shiftKey && send()}
            placeholder={runId ? "Ask questions or inject requirements into the pipeline..." : "Ask about pipelines, agents, security, code..."}
            className="flex-1 px-3 py-2 rounded-lg bg-gray-50 dark:bg-gray-700 border border-gray-200 dark:border-gray-600 text-sm text-gray-900 dark:text-gray-100 placeholder:text-gray-400 dark:placeholder:text-gray-500 focus:outline-none focus:border-indigo-400 dark:focus:border-indigo-600 focus:ring-1 focus:ring-indigo-100 dark:focus:ring-indigo-900 transition-colors"
            disabled={loading}
          />
          <button
            onClick={send}
            disabled={loading || !input.trim()}
            className="px-4 py-2 rounded-lg bg-indigo-600 text-white text-sm font-medium hover:bg-indigo-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            Send
          </button>
        </div>
        <p className="text-xs text-gray-400 dark:text-gray-500 mt-1.5 px-1">
          16 tools: pipelines, A2A messages, memory, security, SRE, source code, PRs, issues, inject requirements
        </p>
      </div>
    </div>
  );
}

/** Minimal markdown to HTML (tables, bold, code, lists). */
function renderMarkdown(md: string): string {
  return md
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/```(\w*)\n([\s\S]*?)```/g, '<pre class="rounded-lg p-3 my-2 overflow-x-auto text-xs"><code>$2</code></pre>')
    .replace(/`([^`]+)`/g, '<code class="px-1.5 py-0.5 rounded text-xs">$1</code>')
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/\*(.+?)\*/g, "<em>$1</em>")
    .replace(/^### (.+)$/gm, '<h3 class="text-sm font-semibold mt-3 mb-1">$1</h3>')
    .replace(/^## (.+)$/gm, '<h2 class="text-base font-semibold mt-4 mb-1">$1</h2>')
    .replace(/^# (.+)$/gm, '<h1 class="text-lg font-bold mt-4 mb-2">$1</h1>')
    .replace(/^\|(.+)\|$/gm, (_, row) => {
      const cells = row.split("|").map((c: string) => c.trim());
      return `<tr>${cells.map((c: string) => `<td class="px-2 py-1 border-b border-gray-200 dark:border-gray-600">${c}</td>`).join("")}</tr>`;
    })
    .replace(/((<tr>.*<\/tr>\n?)+)/g, '<table class="w-full text-xs my-2 border border-gray-200 dark:border-gray-600 rounded">$1</table>')
    .replace(/^- (.+)$/gm, '<li class="ml-4 list-disc">$1</li>')
    .replace(/((<li.*<\/li>\n?)+)/g, '<ul class="my-1">$1</ul>')
    .replace(/\n/g, "<br />");
}
