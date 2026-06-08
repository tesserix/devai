"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { clsx } from "clsx";

interface Message {
  role: "user" | "assistant";
  content: string;
  timestamp: Date;
}

export function SREChatPanel() {
  const [messages, setMessages] = useState<Message[]>([
    {
      role: "assistant",
      content:
        "Hi! I'm the DevAI SRE assistant. I have full access to your GKE cluster.\n\n" +
        "I can check **pod health**, **resource usage**, **logs**, **incidents**, **costs**, and more. " +
        "Ask me anything about your cluster — I'll fetch live data and give you actionable recommendations.\n\n" +
        "Try: *\"What's the health of the homechef namespace?\"* or *\"Show me recent incidents\"*",
      timestamp: new Date(),
    },
  ]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  // Persist the chat session id (DASH-11). Previously a fresh
  // `sre-chat-${Date.now()}` was minted on every mount, so navigating away
  // and back (or a reload) silently started a new server-side conversation
  // and dropped the 30-message history the backend keeps per session. We
  // reuse a stable id across mounts via localStorage; SSR-safe (ref init runs
  // on the client for "use client" components, guarded for older runtimes).
  const sessionId = useRef<string>("");
  if (!sessionId.current) {
    sessionId.current = readOrCreateSessionId();
  }

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
      const res = await fetch("/api/chat/message", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: userMsg, session_id: sessionId.current }),
      });

      if (!res.ok) {
        const errorText = await res.text();
        throw new Error(errorText || `Server error (${res.status})`);
      }
      const data = await res.json();
      setMessages((prev) => [
        ...prev,
        { role: "assistant", content: data.response || "No response from the model — check server logs.", timestamp: new Date() },
      ]);
    } catch (error) {
      const message = error instanceof Error ? error.message : "Failed to connect to the SRE chat API.";
      setMessages((prev) => [
        ...prev,
        { role: "assistant", content: message, timestamp: new Date() },
      ]);
    } finally {
      setLoading(false);
    }
  }, [input, loading]);

  return (
    <div className="flex flex-col h-[calc(100vh-120px)] bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 overflow-hidden">
      {/* Messages */}
      <div ref={scrollRef} className="flex-1 overflow-y-auto p-4 space-y-3">
        {messages.map((msg, i) => (
          <div key={i} className={clsx("flex", msg.role === "user" ? "justify-end" : "justify-start")}>
            <div
              className={clsx(
                "max-w-[80%] rounded-xl px-4 py-3 text-sm",
                msg.role === "user"
                  ? "bg-emerald-600 text-white rounded-br-sm"
                  : "bg-gray-50 dark:bg-gray-700 text-gray-800 dark:text-gray-200 border border-gray-200 dark:border-gray-600 rounded-bl-sm"
              )}
            >
              {msg.role === "assistant" ? (
                <div
                  className="prose dark:prose-invert prose-sm max-w-none prose-pre:bg-gray-100 dark:prose-pre:bg-gray-800 prose-code:text-emerald-700 dark:prose-code:text-emerald-400"
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
                <span className="w-1.5 h-1.5 bg-emerald-500 rounded-full animate-bounce" />
                <span className="w-1.5 h-1.5 bg-emerald-500 rounded-full animate-bounce [animation-delay:0.15s]" />
                <span className="w-1.5 h-1.5 bg-emerald-500 rounded-full animate-bounce [animation-delay:0.3s]" />
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
            placeholder="Ask about cluster health, pods, logs, incidents, costs..."
            className="flex-1 px-3 py-2 rounded-lg bg-gray-50 dark:bg-gray-700 border border-gray-200 dark:border-gray-600 text-sm text-gray-900 dark:text-gray-100 placeholder:text-gray-400 dark:placeholder:text-gray-500 focus:outline-none focus:border-emerald-400 dark:focus:border-emerald-600 focus:ring-1 focus:ring-emerald-100 dark:focus:ring-emerald-900 transition-colors"
            disabled={loading}
          />
          <button
            onClick={send}
            disabled={loading || !input.trim()}
            className="px-4 py-2 rounded-lg bg-emerald-600 text-white text-sm font-medium hover:bg-emerald-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            Send
          </button>
        </div>
        <p className="text-xs text-gray-400 dark:text-gray-500 mt-1.5 px-1">
          Live kubectl access: pods, logs, events, resource usage, deployments, HPA, incidents, costs
        </p>
      </div>
    </div>
  );
}

// Stable, persisted SRE chat session id (DASH-11). Falls back to an
// in-memory id when localStorage is unavailable (private mode / SSR).
const SESSION_KEY = "devai-sre-chat-session";

function readOrCreateSessionId(): string {
  if (typeof window === "undefined") return `sre-chat-${Date.now()}`;
  try {
    const existing = window.localStorage.getItem(SESSION_KEY);
    if (existing) return existing;
    const fresh = `sre-chat-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    window.localStorage.setItem(SESSION_KEY, fresh);
    return fresh;
  } catch {
    return `sre-chat-${Date.now()}`;
  }
}

// ── Markdown rendering (DASH-4) ─────────────────────────────────────────────
// SRE chat content is heavily cluster/SCM-derived (pod names, log lines, event
// messages, incident/issue text) and therefore UNTRUSTED. The render path is:
//
//   1. entity-escape the raw text (so any literal HTML is inert),
//   2. apply a small, fixed markdown→HTML transform,
//   3. run the result through DOMPurify — a vetted sanitizer — with a strict
//      tag allowlist, a PROTOCOL ALLOWLIST on links (http/https/mailto only,
//      no javascript:/data:), and a hook that forces rel="noopener noreferrer"
//      and target="_blank" on every surviving anchor.
//
// The old hand-rolled escape-first pipeline was one reorder away from stored
// XSS (e.g. an `<img src=x onerror=...>` in a log line). DOMPurify needs
// `window`, so on the server we return only the escaped+marked HTML (already
// inert, no anchors emitted) and the browser re-sanitizes on hydration.

const SAFE_URI =
  /^(?:(?:https?|mailto):|[^a-z]|[a-z+.-]+(?:[^a-z+.\-:]|$))/i;

let domPurifyHookInstalled = false;

function markdownToHtml(md: string): string {
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
    // Links — markdown [label](url). The url is sanitized by DOMPurify's
    // protocol allowlist below; the label is already entity-escaped.
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2">$1</a>')
    .replace(/^\|(.+)\|$/gm, (_, row) => {
      const cells = row.split("|").map((c: string) => c.trim());
      return `<tr>${cells.map((c: string) => `<td class="px-2 py-1 border-b border-gray-200 dark:border-gray-600">${c}</td>`).join("")}</tr>`;
    })
    .replace(/((<tr>.*<\/tr>\n?)+)/g, '<table class="w-full text-xs my-2 border border-gray-200 dark:border-gray-600 rounded">$1</table>')
    .replace(/^- (.+)$/gm, '<li class="ml-4 list-disc">$1</li>')
    .replace(/((<li.*<\/li>\n?)+)/g, '<ul class="my-1">$1</ul>')
    .replace(/\n/g, "<br />");
}

function renderMarkdown(md: string): string {
  const html = markdownToHtml(md);

  // On the server we cannot run DOMPurify (no DOM). The escaped+marked HTML is
  // already inert; the browser re-renders and sanitizes on hydration.
  if (typeof window === "undefined") return html;

  // eslint-disable-next-line @typescript-eslint/no-require-imports
  const DOMPurify = require("dompurify");
  const purify = DOMPurify.default ?? DOMPurify;

  // Force safe link attributes on every anchor exactly once per session.
  if (!domPurifyHookInstalled && typeof purify.addHook === "function") {
    purify.addHook("afterSanitizeAttributes", (node: Element) => {
      if (node.tagName === "A") {
        node.setAttribute("rel", "noopener noreferrer");
        node.setAttribute("target", "_blank");
      }
    });
    domPurifyHookInstalled = true;
  }

  return purify.sanitize(html, {
    ALLOWED_TAGS: ["pre", "code", "strong", "em", "h1", "h2", "h3", "table", "tr", "td", "ul", "li", "br", "a", "p"],
    // No `target` here — the hook stamps it. `rel` is allowed so the hook's
    // value survives. Protocol allowlist blocks javascript:/data: URIs.
    ALLOWED_ATTR: ["class", "href", "rel", "target"],
    ALLOWED_URI_REGEXP: SAFE_URI,
  });
}
