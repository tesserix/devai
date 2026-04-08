"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";

interface Repo {
  full_name: string;
  name: string;
  description: string;
  language: string;
  private: boolean;
}

interface TriggerDialogProps {
  open: boolean;
  onClose: () => void;
  onTrigger: (repo: string, requirements: string) => Promise<void>;
}

export function TriggerDialog({ open, onClose, onTrigger }: TriggerDialogProps) {
  const [repos, setRepos] = useState<Repo[]>([]);
  const [search, setSearch] = useState("");
  const [selectedRepo, setSelectedRepo] = useState("");
  const [requirements, setRequirements] = useState("");
  const [loading, setLoading] = useState(false);
  const [loadingRepos, setLoadingRepos] = useState(false);
  const [dropdownOpen, setDropdownOpen] = useState(false);
  const [showCreate, setShowCreate] = useState(false);
  const [newRepoName, setNewRepoName] = useState("");
  const [newRepoDesc, setNewRepoDesc] = useState("");
  const [creating, setCreating] = useState(false);
  const dropdownRef = useRef<HTMLDivElement>(null);

  const fetchRepos = useCallback(async () => {
    setLoadingRepos(true);
    try {
      const data = await api.listRepos();
      setRepos(data);
    } catch {
      // API may not be available
    } finally {
      setLoadingRepos(false);
    }
  }, []);

  useEffect(() => {
    if (open && repos.length === 0) fetchRepos();
  }, [open, repos.length, fetchRepos]);

  // Close dropdown on outside click
  useEffect(() => {
    function handleClick(e: MouseEvent) {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target as Node)) {
        setDropdownOpen(false);
      }
    }
    document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, []);

  if (!open) return null;

  const filtered = repos.filter(
    (r) =>
      r.full_name.toLowerCase().includes(search.toLowerCase()) ||
      r.description?.toLowerCase().includes(search.toLowerCase())
  );

  const handleSelect = (fullName: string) => {
    setSelectedRepo(fullName);
    setSearch(fullName);
    setDropdownOpen(false);
  };

  const handleCreate = async () => {
    if (!newRepoName.trim()) return;
    setCreating(true);
    try {
      const repo = await api.createRepo("tesserix", newRepoName.trim(), newRepoDesc);
      setSelectedRepo(repo.full_name);
      setSearch(repo.full_name);
      setShowCreate(false);
      setNewRepoName("");
      setNewRepoDesc("");
      await fetchRepos();
    } catch {
      // Handle error
    } finally {
      setCreating(false);
    }
  };

  const handleSubmit = async () => {
    if (!selectedRepo || !requirements) return;
    setLoading(true);
    try {
      await onTrigger(selectedRepo, requirements);
      setSelectedRepo("");
      setSearch("");
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
          <h2 className="text-base font-semibold text-gray-900 dark:text-gray-100">
            New Pipeline Run
          </h2>
          <button
            onClick={onClose}
            className="w-7 h-7 flex items-center justify-center rounded-md text-gray-400 dark:text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors"
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
              <line x1="18" y1="6" x2="6" y2="18" />
              <line x1="6" y1="6" x2="18" y2="18" />
            </svg>
          </button>
        </div>
        <p className="text-sm text-gray-500 dark:text-gray-400 mb-5">
          Select a repository and describe your requirements
        </p>

        <div className="space-y-4">
          {/* Repository selector */}
          <div ref={dropdownRef} className="relative">
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1.5">
              Repository
            </label>
            <input
              type="text"
              placeholder={loadingRepos ? "Loading repositories..." : "Search repositories..."}
              value={search}
              onChange={(e) => {
                setSearch(e.target.value);
                setSelectedRepo("");
                setDropdownOpen(true);
              }}
              onFocus={() => setDropdownOpen(true)}
              className="w-full px-3 py-2.5 rounded-lg bg-gray-50 dark:bg-gray-700 border border-gray-200 dark:border-gray-600 text-sm text-gray-900 dark:text-gray-100 placeholder:text-gray-400 dark:placeholder:text-gray-500 focus:outline-none focus:border-indigo-500 focus:ring-1 focus:ring-indigo-200 dark:focus:ring-indigo-900 transition-colors"
            />

            {/* Dropdown */}
            {dropdownOpen && (
              <div className="absolute z-10 w-full mt-1 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-600 rounded-lg shadow-lg max-h-56 overflow-y-auto">
                {filtered.length > 0 ? (
                  filtered.map((r) => (
                    <button
                      key={r.full_name}
                      onClick={() => handleSelect(r.full_name)}
                      className="w-full text-left px-3 py-2.5 hover:bg-gray-50 dark:hover:bg-gray-700 border-b border-gray-100 dark:border-gray-700 last:border-0 transition-colors"
                    >
                      <div className="flex items-center justify-between">
                        <span className="text-sm font-medium text-gray-900 dark:text-gray-100">
                          {r.full_name}
                        </span>
                        <div className="flex items-center gap-2">
                          {r.language && (
                            <span className="text-xs text-gray-400 dark:text-gray-500">
                              {r.language}
                            </span>
                          )}
                          {r.private && (
                            <span className="text-xs px-1.5 py-0.5 rounded bg-gray-100 dark:bg-gray-600 text-gray-500 dark:text-gray-400">
                              Private
                            </span>
                          )}
                        </div>
                      </div>
                      {r.description && (
                        <p className="text-xs text-gray-500 dark:text-gray-400 mt-0.5 truncate">
                          {r.description}
                        </p>
                      )}
                    </button>
                  ))
                ) : (
                  <div className="px-3 py-3 text-sm text-gray-500 dark:text-gray-400">
                    {loadingRepos ? "Loading..." : "No repositories found"}
                  </div>
                )}

                {/* Create new repo option */}
                <button
                  onClick={() => {
                    setShowCreate(true);
                    setDropdownOpen(false);
                  }}
                  className="w-full text-left px-3 py-2.5 text-sm font-medium text-indigo-600 dark:text-indigo-400 hover:bg-indigo-50 dark:hover:bg-indigo-900/20 border-t border-gray-200 dark:border-gray-600 transition-colors"
                >
                  + Create new repository
                </button>
              </div>
            )}
          </div>

          {/* Create repo inline form */}
          {showCreate && (
            <div className="p-4 rounded-lg border border-indigo-200 dark:border-indigo-800 bg-indigo-50/50 dark:bg-indigo-900/10 space-y-3">
              <div className="flex items-center justify-between">
                <span className="text-sm font-medium text-gray-900 dark:text-gray-100">
                  Create Repository
                </span>
                <button
                  onClick={() => setShowCreate(false)}
                  className="text-xs text-gray-400 hover:text-gray-600 dark:hover:text-gray-300"
                >
                  Cancel
                </button>
              </div>
              <div>
                <label className="block text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">
                  Name (tesserix/...)
                </label>
                <input
                  type="text"
                  placeholder="my-new-project"
                  value={newRepoName}
                  onChange={(e) => setNewRepoName(e.target.value)}
                  className="w-full px-3 py-2 rounded-lg bg-white dark:bg-gray-700 border border-gray-200 dark:border-gray-600 text-sm text-gray-900 dark:text-gray-100 placeholder:text-gray-400 focus:outline-none focus:border-indigo-500 transition-colors"
                />
              </div>
              <div>
                <label className="block text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">
                  Description (optional)
                </label>
                <input
                  type="text"
                  placeholder="Brief description"
                  value={newRepoDesc}
                  onChange={(e) => setNewRepoDesc(e.target.value)}
                  className="w-full px-3 py-2 rounded-lg bg-white dark:bg-gray-700 border border-gray-200 dark:border-gray-600 text-sm text-gray-900 dark:text-gray-100 placeholder:text-gray-400 focus:outline-none focus:border-indigo-500 transition-colors"
                />
              </div>
              <button
                onClick={handleCreate}
                disabled={creating || !newRepoName.trim()}
                className="px-4 py-2 text-sm font-medium rounded-lg bg-indigo-600 text-white hover:bg-indigo-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
              >
                {creating ? "Creating..." : "Create"}
              </button>
            </div>
          )}

          {/* Requirements */}
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1.5">
              Requirements
            </label>
            <textarea
              rows={5}
              placeholder="Describe the feature or requirements..."
              value={requirements}
              onChange={(e) => setRequirements(e.target.value)}
              className="w-full px-3 py-2.5 rounded-lg bg-gray-50 dark:bg-gray-700 border border-gray-200 dark:border-gray-600 text-sm text-gray-900 dark:text-gray-100 placeholder:text-gray-400 dark:placeholder:text-gray-500 focus:outline-none focus:border-indigo-500 focus:ring-1 focus:ring-indigo-200 dark:focus:ring-indigo-900 resize-none transition-colors"
            />
          </div>
        </div>

        <div className="mt-5 flex justify-end gap-2">
          <button
            onClick={onClose}
            className="px-4 py-2 text-sm font-medium rounded-lg text-gray-600 dark:text-gray-400 border border-gray-200 dark:border-gray-600 hover:bg-gray-50 dark:hover:bg-gray-700 transition-colors"
          >
            Cancel
          </button>
          <button
            onClick={handleSubmit}
            disabled={loading || !selectedRepo || !requirements}
            className="px-4 py-2 text-sm font-medium rounded-lg bg-indigo-600 text-white hover:bg-indigo-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            {loading ? "Starting..." : "Start Pipeline"}
          </button>
        </div>
      </div>
    </div>
  );
}
