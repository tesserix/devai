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
  notification: "bg-gray-100 dark:bg-slate-800 text-gray-600 dark:text-slate-400 border border-gray-200 dark:border-slate-700",
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
      <div className="text-center py-8 text-gray-400 dark:text-slate-500 text-sm">
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
            className="flex gap-3 p-3 rounded-lg bg-white dark:bg-slate-900 border border-gray-200 dark:border-slate-800 hover:border-gray-300 dark:hover:border-slate-700 transition-colors"
          >
            <div className="flex-shrink-0 pt-0.5">
              <div
                className="w-6 h-6 rounded flex items-center justify-center text-[10px] font-semibold"
                style={{ backgroundColor: `${from?.color ?? "#64748b"}18`, color: from?.color ?? "#64748b" }}
              >
                {from?.label?.charAt(0) ?? "?"}
              </div>
            </div>
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2 text-xs">
                <span className="font-medium text-gray-800 dark:text-slate-200">
                  {from?.label || msg.from_agent}
                </span>
                <span className="text-gray-300 dark:text-slate-600">&rarr;</span>
                <span className="text-gray-600 dark:text-slate-400">
                  {to?.label || msg.to_agent}
                </span>
                <span className={`font-mono text-[10px] px-1.5 py-0.5 rounded ${typeStyle}`}>
                  {typeLabel}
                </span>
              </div>
              <p className="text-xs font-medium text-gray-800 dark:text-slate-200 mt-1">
                {msg.subject}
              </p>
              <p className="text-[11px] text-gray-400 dark:text-slate-500 mt-0.5 line-clamp-2">
                {msg.body}
              </p>
            </div>
            <div className="flex-shrink-0 text-[10px] text-gray-400 dark:text-slate-500 whitespace-nowrap">
              {new Date(msg.timestamp).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
            </div>
          </div>
        );
      })}
    </div>
  );
}
