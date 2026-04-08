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
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm"
      onClick={(e) => e.target === e.currentTarget && onClose()}
    >
      <div className="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-xl w-full max-w-lg p-6 shadow-xl">
        <div className="flex items-center justify-between mb-1">
          <h2 className="text-base font-semibold text-gray-900 dark:text-gray-100">New Pipeline Run</h2>
          <button
            onClick={onClose}
            className="w-7 h-7 flex items-center justify-center rounded-md text-gray-400 dark:text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors"
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
              <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
            </svg>
          </button>
        </div>
        <p className="text-xs text-gray-400 dark:text-gray-500 mb-5">
          Trigger the full ALM pipeline with LangGraph orchestration
        </p>

        <div className="space-y-4">
          <div>
            <label className="block text-xs font-medium text-gray-600 dark:text-gray-400 mb-1.5">
              Repository
            </label>
            <input
              type="text"
              placeholder="tesserix/my-repo"
              value={repo}
              onChange={(e) => setRepo(e.target.value)}
              className="w-full px-3 py-2 rounded-lg bg-gray-50 dark:bg-gray-700 border border-gray-200 dark:border-gray-600 text-sm text-gray-900 dark:text-gray-100 placeholder:text-gray-400 dark:placeholder:text-gray-500 focus:outline-none focus:border-indigo-400 dark:focus:border-indigo-600 focus:ring-1 focus:ring-indigo-200 dark:focus:ring-indigo-900 transition-colors"
            />
          </div>

          <div>
            <label className="block text-xs font-medium text-gray-600 dark:text-gray-400 mb-1.5">
              Requirements
            </label>
            <textarea
              rows={6}
              placeholder="Describe the feature or requirements..."
              value={requirements}
              onChange={(e) => setRequirements(e.target.value)}
              className="w-full px-3 py-2 rounded-lg bg-gray-50 dark:bg-gray-700 border border-gray-200 dark:border-gray-600 text-sm text-gray-900 dark:text-gray-100 placeholder:text-gray-400 dark:placeholder:text-gray-500 focus:outline-none focus:border-indigo-400 dark:focus:border-indigo-600 focus:ring-1 focus:ring-indigo-200 dark:focus:ring-indigo-900 resize-none transition-colors"
            />
          </div>
        </div>

        <div className="mt-5 flex justify-end gap-2">
          <button
            onClick={onClose}
            className="px-4 py-2 text-xs font-medium rounded-lg text-gray-600 dark:text-gray-400 border border-gray-200 dark:border-gray-600 hover:bg-gray-50 dark:hover:bg-gray-700 transition-colors"
          >
            Cancel
          </button>
          <button
            onClick={handleSubmit}
            disabled={loading || !repo || !requirements}
            className="px-4 py-2 text-xs font-medium rounded-lg bg-indigo-600 text-white hover:bg-indigo-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            {loading ? "Starting..." : "Start Pipeline"}
          </button>
        </div>
      </div>
    </div>
  );
}
