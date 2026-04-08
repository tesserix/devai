"use client";

import { AGENT_INFO } from "@/lib/constants";

interface Approval {
  gate: string;
  agent: string;
  timestamp: number;
}

interface ApprovalBannerProps {
  approvals: Approval[];
  onApprove: (gate: string) => void;
  onReject: (gate: string) => void;
}

export function ApprovalBanner({ approvals, onApprove, onReject }: ApprovalBannerProps) {
  if (approvals.length === 0) return null;

  return (
    <div className="space-y-2">
      {approvals.map((a) => {
        const agent = AGENT_INFO[a.agent];
        return (
          <div
            key={a.gate}
            className="flex items-center justify-between p-3 rounded-lg border border-amber-200 dark:border-amber-900 bg-amber-50 dark:bg-amber-950/30"
          >
            <div className="flex items-center gap-3">
              <div className="w-8 h-8 rounded-lg bg-amber-100 dark:bg-amber-900/40 flex items-center justify-center shrink-0">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="text-amber-600 dark:text-amber-400">
                  <circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/>
                </svg>
              </div>
              <div>
                <p className="text-sm font-medium text-amber-700 dark:text-amber-400">
                  Approval Required: {a.gate}
                </p>
                <p className="text-xs text-gray-500 dark:text-slate-400">
                  {agent?.label || a.agent} is waiting for your decision
                </p>
              </div>
            </div>
            <div className="flex gap-2 shrink-0">
              <button
                onClick={() => onReject(a.gate)}
                className="px-3 py-1.5 text-xs font-medium rounded-md border border-red-200 dark:border-red-800 text-red-600 dark:text-red-400 hover:bg-red-50 dark:hover:bg-red-950/30 transition-colors"
              >
                Reject
              </button>
              <button
                onClick={() => onApprove(a.gate)}
                className="px-3 py-1.5 text-xs font-medium rounded-md bg-green-600 text-white hover:bg-green-700 transition-colors"
              >
                Approve
              </button>
            </div>
          </div>
        );
      })}
    </div>
  );
}
