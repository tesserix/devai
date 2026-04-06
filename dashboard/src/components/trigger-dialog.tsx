"use client";

import { useState } from "react";

interface TriggerDialogProps {
  open: boolean;
  onClose: () => void;
  onTrigger: (repo: string, requirements: string) => Promise<void>;
}

export function TriggerDialog({ open, onClose, onTrigger }: TriggerDialogProps) {
  const [repo, setRepo] = useState("");
  const [requirements, setRequirements] = useState("");
  const [loading, setLoading] = useState(false);

  if (!open) return null;

  const handleSubmit = async () => {
    if (!repo || !requirements) return;
    setLoading(true);
    try {
      await onTrigger(repo, requirements);
      setRepo("");
      setRequirements("");
      onClose();
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
      <div className="bg-[var(--bg-secondary)] border border-[var(--border-primary)] rounded-xl w-full max-w-lg p-6 shadow-2xl">
        <h2 className="text-lg font-semibold text-[var(--text-primary)]">New Pipeline Run</h2>
        <p className="text-xs text-[var(--text-muted)] mt-1">
          Trigger the full ALM pipeline with LangGraph orchestration
        </p>

        <div className="mt-5 space-y-4">
          <div>
            <label className="block text-xs font-medium text-[var(--text-secondary)] mb-1.5">
              Repository
            </label>
            <input
              type="text"
              placeholder="tesserix/my-repo"
              value={repo}
              onChange={(e) => setRepo(e.target.value)}
              className="w-full px-3 py-2 rounded-lg bg-[var(--bg-tertiary)] border border-[var(--border-primary)] text-sm text-[var(--text-primary)] placeholder:text-[var(--text-muted)] focus:outline-none focus:border-blue-500/50 focus:ring-1 focus:ring-blue-500/20"
            />
          </div>

          <div>
            <label className="block text-xs font-medium text-[var(--text-secondary)] mb-1.5">
              Requirements
            </label>
            <textarea
              rows={6}
              placeholder="Describe the feature or requirements..."
              value={requirements}
              onChange={(e) => setRequirements(e.target.value)}
              className="w-full px-3 py-2 rounded-lg bg-[var(--bg-tertiary)] border border-[var(--border-primary)] text-sm text-[var(--text-primary)] placeholder:text-[var(--text-muted)] focus:outline-none focus:border-blue-500/50 focus:ring-1 focus:ring-blue-500/20 resize-none"
            />
          </div>
        </div>

        <div className="mt-6 flex justify-end gap-3">
          <button
            onClick={onClose}
            className="px-4 py-2 text-xs font-medium rounded-lg text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)] transition-colors"
          >
            Cancel
          </button>
          <button
            onClick={handleSubmit}
            disabled={loading || !repo || !requirements}
            className="px-4 py-2 text-xs font-medium rounded-lg bg-blue-600 text-white hover:bg-blue-500 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            {loading ? "Starting..." : "Start Pipeline"}
          </button>
        </div>
      </div>
    </div>
  );
}
