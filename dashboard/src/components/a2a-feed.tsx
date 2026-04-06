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

const TYPE_COLORS: Record<string, string> = {
  request: "text-blue-400",
  response: "text-green-400",
  notification: "text-gray-400",
  handoff: "text-purple-400",
  escalation: "text-red-400",
  clarification: "text-amber-400",
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
      <div className="text-center py-8 text-[var(--text-muted)] text-sm">
        No agent communication yet
      </div>
    );
  }

  return (
    <div className="space-y-2 max-h-[400px] overflow-y-auto">
      {messages.map((msg) => {
        const from = AGENT_INFO[msg.from_agent];
        const to = AGENT_INFO[msg.to_agent];
        const typeColor = TYPE_COLORS[msg.message_type] || "text-gray-400";
        const typeLabel = TYPE_LABELS[msg.message_type] || msg.message_type;

        return (
          <div
            key={msg.id}
            className="flex gap-3 p-2.5 rounded-md bg-[var(--bg-secondary)] border border-[var(--border-primary)] hover:border-[var(--border-secondary)] transition-colors"
          >
            <div className="flex-shrink-0 pt-0.5">
              <span className="text-sm">{from?.icon || "?"}</span>
            </div>
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2 text-xs">
                <span className="font-medium text-[var(--text-primary)]">
                  {from?.label || msg.from_agent}
                </span>
                <span className="text-[var(--text-muted)]">→</span>
                <span className="text-[var(--text-secondary)]">
                  {to?.label || msg.to_agent}
                </span>
                <span className={`${typeColor} font-mono text-[10px] px-1.5 py-0.5 rounded bg-[var(--bg-tertiary)]`}>
                  {typeLabel}
                </span>
              </div>
              <p className="text-xs font-medium text-[var(--text-primary)] mt-1">
                {msg.subject}
              </p>
              <p className="text-[11px] text-[var(--text-muted)] mt-0.5 line-clamp-2">
                {msg.body}
              </p>
            </div>
            <div className="flex-shrink-0 text-[10px] text-[var(--text-muted)]">
              {new Date(msg.timestamp).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
            </div>
          </div>
        );
      })}
    </div>
  );
}
