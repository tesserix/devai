"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { clsx } from "clsx";

interface Message {
  role: "user" | "assistant";
  content: string;
  timestamp: Date;
}

export function ChatPanel() {
  const [messages, setMessages] = useState<Message[]>([
    {
      role: "assistant",
      content:
        "Hi! I'm the DevAI assistant. I can query pipeline runs, agent memory, security findings, SRE incidents, source code, and more. What would you like to know?",
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
    const userMsg = input.trim();
    setInput("");

    setMessages((prev) => [...prev, { role: "user", content: userMsg, timestamp: new Date() }]);
    setLoading(true);

    try {
      const res = await fetch("/chat/api/message", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ message: userMsg, session_id: sessionId.current }),
      });

      const data = await res.json();
      setMessages((prev) => [
        ...prev,
        { role: "assistant", content: data.response || "No response.", timestamp: new Date() },
      ]);
    } catch {
      setMessages((prev) => [
        ...prev,
        { role: "assistant", content: "Failed to connect to the chat API.", timestamp: new Date() },
      ]);
    } finally {
      setLoading(false);
    }
  }, [input, loading]);

  return (
    <div className="flex flex-col h-full">
      {/* Messages */}
      <div ref={scrollRef} className="flex-1 overflow-y-auto p-4 space-y-4">
        {messages.map((msg, i) => (
          <div key={i} className={clsx("flex", msg.role === "user" ? "justify-end" : "justify-start")}>
            <div
              className={clsx(
                "max-w-[80%] rounded-xl px-4 py-3 text-sm",
                msg.role === "user"
                  ? "bg-blue-600 text-white rounded-br-sm"
                  : "bg-[var(--bg-tertiary)] text-[var(--text-primary)] border border-[var(--border-primary)] rounded-bl-sm"
              )}
            >
              {msg.role === "assistant" ? (
                <div
                  className="prose prose-invert prose-sm max-w-none [&_table]:text-xs [&_pre]:bg-[var(--bg-primary)] [&_code]:text-emerald-400"
                  dangerouslySetInnerHTML={{ __html: renderMarkdown(msg.content) }}
                />
              ) : (
                <p className="whitespace-pre-wrap">{msg.content}</p>
              )}
              <p className="text-[9px] mt-1.5 opacity-50">
                {msg.timestamp.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
              </p>
            </div>
          </div>
        ))}
        {loading && (
          <div className="flex justify-start">
            <div className="bg-[var(--bg-tertiary)] border border-[var(--border-primary)] rounded-xl rounded-bl-sm px-4 py-3">
              <div className="flex items-center gap-1.5">
                <span className="w-2 h-2 bg-blue-500 rounded-full animate-bounce" />
                <span className="w-2 h-2 bg-blue-500 rounded-full animate-bounce [animation-delay:0.15s]" />
                <span className="w-2 h-2 bg-blue-500 rounded-full animate-bounce [animation-delay:0.3s]" />
              </div>
            </div>
          </div>
        )}
      </div>

      {/* Input */}
      <div className="border-t border-[var(--border-primary)] p-3">
        <div className="flex gap-2">
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && !e.shiftKey && send()}
            placeholder="Ask about pipelines, agents, security, code..."
            className="flex-1 px-3 py-2.5 rounded-lg bg-[var(--bg-tertiary)] border border-[var(--border-primary)] text-sm text-[var(--text-primary)] placeholder:text-[var(--text-muted)] focus:outline-none focus:border-blue-500/50"
            disabled={loading}
          />
          <button
            onClick={send}
            disabled={loading || !input.trim()}
            className="px-4 py-2.5 rounded-lg bg-blue-600 text-white text-sm font-medium hover:bg-blue-500 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            Send
          </button>
        </div>
        <p className="text-[10px] text-[var(--text-muted)] mt-1.5 px-1">
          15 tools available: pipelines, A2A messages, memory, security, SRE, source code, PRs, issues
        </p>
      </div>
    </div>
  );
}

/** Minimal markdown → HTML (tables, bold, code, lists). */
function renderMarkdown(md: string): string {
  return md
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    // Code blocks
    .replace(/```(\w*)\n([\s\S]*?)```/g, '<pre class="rounded-lg p-3 my-2 overflow-x-auto"><code>$2</code></pre>')
    // Inline code
    .replace(/`([^`]+)`/g, '<code class="px-1.5 py-0.5 rounded bg-[var(--bg-primary)] text-xs">$1</code>')
    // Bold
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    // Italic
    .replace(/\*(.+?)\*/g, "<em>$1</em>")
    // Headers
    .replace(/^### (.+)$/gm, '<h3 class="text-sm font-semibold mt-3 mb-1">$1</h3>')
    .replace(/^## (.+)$/gm, '<h2 class="text-base font-semibold mt-4 mb-1">$1</h2>')
    .replace(/^# (.+)$/gm, '<h1 class="text-lg font-bold mt-4 mb-2">$1</h1>')
    // Table rows
    .replace(/^\|(.+)\|$/gm, (_, row) => {
      const cells = row.split("|").map((c: string) => c.trim());
      return `<tr>${cells.map((c: string) => `<td class="px-2 py-1 border-b border-[var(--border-primary)]">${c}</td>`).join("")}</tr>`;
    })
    // Wrap consecutive <tr> in table
    .replace(/((<tr>.*<\/tr>\n?)+)/g, '<table class="w-full text-xs my-2 border border-[var(--border-primary)]">$1</table>')
    // Unordered lists
    .replace(/^- (.+)$/gm, '<li class="ml-4 list-disc">$1</li>')
    .replace(/((<li.*<\/li>\n?)+)/g, '<ul class="my-1">$1</ul>')
    // Line breaks
    .replace(/\n/g, "<br />");
}
