"use client";

import { AGENT_INFO } from "@/lib/constants";

interface A2AMessage {
  id: string;
  from_agent: string;
  to_agent: string;
  message_type: string;
  subject: string;
  body: string;
  timestamp: string;
}

interface A2AFeedProps {
  messages: A2AMessage[];
}

const TYPE_STYLES: Record<string, string> = {
  request: "bg-blue-50 dark:bg-blue-950 text-blue-700 dark:text-blue-400 border border-blue-100 dark:border-blue-900",
  response: "bg-green-50 dark:bg-green-950 text-green-700 dark:text-green-400 border border-green-100 dark:border-green-900",
  notification: "bg-[var(--surface-hover)] text-[var(--ink-soft)] border border-[var(--border)]",
  handoff: "bg-indigo-50 dark:bg-indigo-950 text-indigo-700 dark:text-indigo-400 border border-indigo-100 dark:border-indigo-900",
  escalation: "bg-red-50 dark:bg-red-950 text-red-700 dark:text-red-400 border border-red-100 dark:border-red-900",
  clarification: "bg-amber-50 dark:bg-amber-950 text-amber-700 dark:text-amber-400 border border-amber-100 dark:border-amber-900",
};

const TYPE_LABELS: Record<string, string> = {
  request: "REQ",
  response: "RES",
  notification: "INFO",
  handoff: "HAND",
  escalation: "ESC",
  clarification: "CLR",
};

export function A2AFeed({ messages }: A2AFeedProps) {
  if (messages.length === 0) {
    return (
      <div className="text-center py-8 text-[var(--ink-muted)] text-sm">
        No agent communication yet
      </div>
    );
  }

  return (
    <div className="space-y-1.5 max-h-[400px] overflow-y-auto">
      {messages.map((msg) => {
        const from = AGENT_INFO[msg.from_agent];
        const to = AGENT_INFO[msg.to_agent];
        const typeStyle = TYPE_STYLES[msg.message_type] || TYPE_STYLES.notification;
        const typeLabel = TYPE_LABELS[msg.message_type] || msg.message_type;

        return (
          <div
            key={msg.id}
            className="flex gap-3 p-3 rounded-lg bg-[var(--surface)] border border-[var(--border)] hover:border-[var(--border-strong)] transition-colors"
          >
            <div className="flex-shrink-0 pt-0.5">
              <div
                className="w-6 h-6 rounded flex items-center justify-center text-xs font-semibold"
                style={{ backgroundColor: `${from?.color ?? "#64748b"}18`, color: from?.color ?? "#64748b" }}
              >
                {from?.label?.charAt(0) ?? "?"}
              </div>
            </div>
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2 text-xs">
                <span className="font-medium text-[var(--ink)]">
                  {from?.label || msg.from_agent}
                </span>
                <span className="text-[var(--ink-disabled)]">&rarr;</span>
                <span className="text-[var(--ink-soft)]">
                  {to?.label || msg.to_agent}
                </span>
                <span className={`font-mono text-xs px-1.5 py-0.5 rounded ${typeStyle}`}>
                  {typeLabel}
                </span>
              </div>
              <p className="text-xs font-medium text-[var(--ink)] mt-1">
                {msg.subject}
              </p>
              <p className="text-xs text-[var(--ink-muted)] mt-0.5 line-clamp-2">
                {msg.body}
              </p>
            </div>
            <div className="flex-shrink-0 text-xs text-[var(--ink-muted)] whitespace-nowrap">
              {new Date(msg.timestamp).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
            </div>
          </div>
        );
      })}
    </div>
  );
}
