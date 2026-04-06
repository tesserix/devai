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
            className="flex items-center justify-between p-3 rounded-lg border border-amber-500/30 bg-amber-500/5"
          >
            <div className="flex items-center gap-3">
              <div className="w-8 h-8 rounded-full bg-amber-500/15 flex items-center justify-center text-sm">
                {agent?.icon || "⏳"}
              </div>
              <div>
                <p className="text-sm font-medium text-amber-400">
                  Approval Required: {a.gate}
                </p>
                <p className="text-xs text-[var(--text-muted)]">
                  {agent?.label || a.agent} is waiting for your decision
                </p>
              </div>
            </div>
            <div className="flex gap-2">
              <button
                onClick={() => onReject(a.gate)}
                className="px-3 py-1.5 text-xs font-medium rounded-md border border-red-500/30 text-red-400 hover:bg-red-500/10 transition-colors"
              >
                Reject
              </button>
              <button
                onClick={() => onApprove(a.gate)}
                className="px-3 py-1.5 text-xs font-medium rounded-md bg-green-600 text-white hover:bg-green-500 transition-colors"
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
