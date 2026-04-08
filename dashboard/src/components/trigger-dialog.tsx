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

interface Project {
  id: string;
  title: string;
  number: number;
  description: string;
  url: string;
}

interface TriggerDialogProps {
  open: boolean;
  onClose: () => void;
  onTrigger: (repo: string, requirements: string) => Promise<void>;
}

export function TriggerDialog({ open, onClose, onTrigger }: TriggerDialogProps) {
  const [repos, setRepos] = useState<Repo[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [search, setSearch] = useState("");
  const [selectedRepo, setSelectedRepo] = useState("");
  const [selectedProject, setSelectedProject] = useState("");
  const [requirements, setRequirements] = useState("");
  const [loading, setLoading] = useState(false);
  const [loadingRepos, setLoadingRepos] = useState(false);
  const [loadingProjects, setLoadingProjects] = useState(false);
  const [dropdownOpen, setDropdownOpen] = useState(false);
  const [projectDropdownOpen, setProjectDropdownOpen] = useState(false);
  const [projectSearch, setProjectSearch] = useState("");

  // Create repo form
  const [showCreate, setShowCreate] = useState(false);
  const [newRepoName, setNewRepoName] = useState("");
  const [newRepoDesc, setNewRepoDesc] = useState("");
  const [creating, setCreating] = useState(false);
  const [repoNameStatus, setRepoNameStatus] = useState<"idle" | "checking" | "available" | "taken">("idle");
  const checkTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Create project form
  const [showCreateProject, setShowCreateProject] = useState(false);
  const [newProjectName, setNewProjectName] = useState("");
  const [newProjectDesc, setNewProjectDesc] = useState("");
  const [creatingProject, setCreatingProject] = useState(false);

  // Scaffold
  const [scaffold, setScaffold] = useState(false);
  const [scaffolding, setScaffolding] = useState(false);

  const dropdownRef = useRef<HTMLDivElement>(null);
  const projectDropdownRef = useRef<HTMLDivElement>(null);

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

  const fetchProjects = useCallback(async () => {
    setLoadingProjects(true);
    try {
      const data = await api.listProjects();
      setProjects(data);
    } catch {
      // API may not be available
    } finally {
      setLoadingProjects(false);
    }
  }, []);

  useEffect(() => {
    if (open && repos.length === 0) fetchRepos();
    if (open && projects.length === 0) fetchProjects();
  }, [open, repos.length, projects.length, fetchRepos, fetchProjects]);

  // Debounced repo name availability check
  const checkRepoName = useCallback((name: string) => {
    if (checkTimerRef.current) clearTimeout(checkTimerRef.current);
    if (!name.trim()) {
      setRepoNameStatus("idle");
      return;
    }
    setRepoNameStatus("checking");
    checkTimerRef.current = setTimeout(async () => {
      try {
        const result = await api.checkRepoName(name.trim());
        setRepoNameStatus(result.available ? "available" : "taken");
      } catch {
        setRepoNameStatus("idle");
      }
    }, 500);
  }, []);

  // Close dropdowns on outside click
  useEffect(() => {
    function handleClick(e: MouseEvent) {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target as Node)) {
        setDropdownOpen(false);
      }
      if (projectDropdownRef.current && !projectDropdownRef.current.contains(e.target as Node)) {
        setProjectDropdownOpen(false);
      }
    }
    document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, []);

  if (!open) return null;

  const filteredRepos = repos.filter(
    (r) =>
      r.full_name.toLowerCase().includes(search.toLowerCase()) ||
      r.description?.toLowerCase().includes(search.toLowerCase())
  );

  const filteredProjects = projects.filter(
    (p) =>
      p.title.toLowerCase().includes(projectSearch.toLowerCase()) ||
      p.description?.toLowerCase().includes(projectSearch.toLowerCase())
  );

  const handleSelectRepo = (fullName: string) => {
    setSelectedRepo(fullName);
    setSearch(fullName);
    setDropdownOpen(false);
  };

  const handleSelectProject = (project: Project) => {
    setSelectedProject(project.title);
    setProjectSearch(project.title);
    setProjectDropdownOpen(false);
  };

  const handleCreateRepo = async () => {
    if (!newRepoName.trim()) return;
    setCreating(true);
    try {
      const repo = await api.createRepo("tesserix", newRepoName.trim(), newRepoDesc);
      setSelectedRepo(repo.full_name);
      setSearch(repo.full_name);
      setShowCreate(false);
      setNewRepoName("");
      setNewRepoDesc("");
      setScaffold(true);
      await fetchRepos();
    } catch {
      // Handle error
    } finally {
      setCreating(false);
    }
  };

  const handleCreateProject = async () => {
    if (!newProjectName.trim()) return;
    setCreatingProject(true);
    try {
      const project = await api.createProject(
        newProjectName.trim(),
        newProjectDesc,
        selectedRepo || undefined
      );
      setSelectedProject(project.title);
      setProjectSearch(project.title);
      setShowCreateProject(false);
      setNewProjectName("");
      setNewProjectDesc("");
      await fetchProjects();
    } catch {
      // Handle error
    } finally {
      setCreatingProject(false);
    }
  };

  const handleSubmit = async () => {
    if (!selectedRepo || !requirements) return;
    setLoading(true);
    try {
      // Scaffold if requested
      if (scaffold) {
        setScaffolding(true);
        try {
          await api.scaffoldRepo(
            selectedRepo,
            selectedProject || undefined,
          );
        } catch {
          // Scaffold is best-effort
        }
        setScaffolding(false);
      }

      await onTrigger(selectedRepo, requirements);
      setSelectedRepo("");
      setSearch("");
      setSelectedProject("");
      setProjectSearch("");
      setRequirements("");
      setScaffold(false);
      onClose();
    } finally {
      setLoading(false);
    }
  };

  const inputClass =
    "w-full px-3 py-2.5 rounded-lg bg-gray-50 dark:bg-gray-700 border border-gray-200 dark:border-gray-600 text-sm text-gray-900 dark:text-gray-100 placeholder:text-gray-400 dark:placeholder:text-gray-500 focus:outline-none focus:border-indigo-500 focus:ring-1 focus:ring-indigo-200 dark:focus:ring-indigo-900 transition-colors";

  const dropdownItemClass =
    "w-full text-left px-3 py-2.5 hover:bg-gray-50 dark:hover:bg-gray-700 border-b border-gray-100 dark:border-gray-700 last:border-0 transition-colors";

  const dropdownContainerClass =
    "absolute z-10 w-full mt-1 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-600 rounded-lg shadow-lg max-h-56 overflow-y-auto";

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm"
      onClick={(e) => e.target === e.currentTarget && onClose()}
    >
      <div className="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-xl w-full max-w-lg p-6 shadow-xl max-h-[90vh] overflow-y-auto">
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
          Select a repository, link a project board, and describe your requirements
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
              className={inputClass}
            />

            {dropdownOpen && (
              <div className={dropdownContainerClass}>
                {filteredRepos.length > 0 ? (
                  filteredRepos.map((r) => (
                    <button
                      key={r.full_name}
                      onClick={() => handleSelectRepo(r.full_name)}
                      className={dropdownItemClass}
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
                <div className="relative">
                  <input
                    type="text"
                    placeholder="my-new-project"
                    value={newRepoName}
                    onChange={(e) => {
                      setNewRepoName(e.target.value);
                      checkRepoName(e.target.value);
                    }}
                    className={`w-full px-3 py-2 rounded-lg bg-white dark:bg-gray-700 border text-sm text-gray-900 dark:text-gray-100 placeholder:text-gray-400 focus:outline-none transition-colors ${
                      repoNameStatus === "taken"
                        ? "border-red-400 focus:border-red-500"
                        : repoNameStatus === "available"
                          ? "border-green-400 focus:border-green-500"
                          : "border-gray-200 dark:border-gray-600 focus:border-indigo-500"
                    }`}
                  />
                  {repoNameStatus === "checking" && (
                    <span className="absolute right-3 top-2.5 text-xs text-gray-400">checking...</span>
                  )}
                  {repoNameStatus === "available" && (
                    <span className="absolute right-3 top-2.5 text-xs text-green-500">available</span>
                  )}
                  {repoNameStatus === "taken" && (
                    <span className="absolute right-3 top-2.5 text-xs text-red-500">already exists</span>
                  )}
                </div>
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
                onClick={handleCreateRepo}
                disabled={creating || !newRepoName.trim() || repoNameStatus === "taken" || repoNameStatus === "checking"}
                className="px-4 py-2 text-sm font-medium rounded-lg bg-indigo-600 text-white hover:bg-indigo-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
              >
                {creating ? "Creating..." : "Create"}
              </button>
            </div>
          )}

          {/* Project selector */}
          <div ref={projectDropdownRef} className="relative">
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1.5">
              Project Board
              <span className="ml-1 text-xs font-normal text-gray-400">(optional)</span>
            </label>
            <input
              type="text"
              placeholder={loadingProjects ? "Loading projects..." : "Search or create a project..."}
              value={projectSearch}
              onChange={(e) => {
                setProjectSearch(e.target.value);
                setSelectedProject("");
                setProjectDropdownOpen(true);
              }}
              onFocus={() => setProjectDropdownOpen(true)}
              className={inputClass}
            />

            {projectDropdownOpen && (
              <div className={dropdownContainerClass}>
                {filteredProjects.length > 0 ? (
                  filteredProjects.map((p) => (
                    <button
                      key={p.id}
                      onClick={() => handleSelectProject(p)}
                      className={dropdownItemClass}
                    >
                      <div className="flex items-center justify-between">
                        <span className="text-sm font-medium text-gray-900 dark:text-gray-100">
                          {p.title}
                        </span>
                        <span className="text-xs text-gray-400 dark:text-gray-500">
                          #{p.number}
                        </span>
                      </div>
                      {p.description && (
                        <p className="text-xs text-gray-500 dark:text-gray-400 mt-0.5 truncate">
                          {p.description}
                        </p>
                      )}
                    </button>
                  ))
                ) : (
                  <div className="px-3 py-3 text-sm text-gray-500 dark:text-gray-400">
                    {loadingProjects ? "Loading..." : "No projects found"}
                  </div>
                )}
                <button
                  onClick={() => {
                    setShowCreateProject(true);
                    setProjectDropdownOpen(false);
                  }}
                  className="w-full text-left px-3 py-2.5 text-sm font-medium text-indigo-600 dark:text-indigo-400 hover:bg-indigo-50 dark:hover:bg-indigo-900/20 border-t border-gray-200 dark:border-gray-600 transition-colors"
                >
                  + Create new project board
                </button>
              </div>
            )}
          </div>

          {/* Create project inline form */}
          {showCreateProject && (
            <div className="p-4 rounded-lg border border-indigo-200 dark:border-indigo-800 bg-indigo-50/50 dark:bg-indigo-900/10 space-y-3">
              <div className="flex items-center justify-between">
                <span className="text-sm font-medium text-gray-900 dark:text-gray-100">
                  Create Project Board
                </span>
                <button
                  onClick={() => setShowCreateProject(false)}
                  className="text-xs text-gray-400 hover:text-gray-600 dark:hover:text-gray-300"
                >
                  Cancel
                </button>
              </div>
              <div>
                <label className="block text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">
                  Project Title
                </label>
                <input
                  type="text"
                  placeholder="Sprint 1 — Feature X"
                  value={newProjectName}
                  onChange={(e) => setNewProjectName(e.target.value)}
                  className="w-full px-3 py-2 rounded-lg bg-white dark:bg-gray-700 border border-gray-200 dark:border-gray-600 text-sm text-gray-900 dark:text-gray-100 placeholder:text-gray-400 focus:outline-none focus:border-indigo-500 transition-colors"
                />
              </div>
              <div>
                <label className="block text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">
                  Description (optional)
                </label>
                <input
                  type="text"
                  placeholder="What is this project about?"
                  value={newProjectDesc}
                  onChange={(e) => setNewProjectDesc(e.target.value)}
                  className="w-full px-3 py-2 rounded-lg bg-white dark:bg-gray-700 border border-gray-200 dark:border-gray-600 text-sm text-gray-900 dark:text-gray-100 placeholder:text-gray-400 focus:outline-none focus:border-indigo-500 transition-colors"
                />
              </div>
              <button
                onClick={handleCreateProject}
                disabled={creatingProject || !newProjectName.trim()}
                className="px-4 py-2 text-sm font-medium rounded-lg bg-indigo-600 text-white hover:bg-indigo-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
              >
                {creatingProject ? "Creating..." : "Create Project"}
              </button>
            </div>
          )}

          {/* Scaffold toggle */}
          <div className="flex items-start gap-3 p-3 rounded-lg bg-gray-50 dark:bg-gray-700/50 border border-gray-200 dark:border-gray-600">
            <input
              type="checkbox"
              id="scaffold"
              checked={scaffold}
              onChange={(e) => setScaffold(e.target.checked)}
              className="mt-0.5 h-4 w-4 rounded border-gray-300 text-indigo-600 focus:ring-indigo-500"
            />
            <label htmlFor="scaffold" className="cursor-pointer">
              <span className="block text-sm font-medium text-gray-900 dark:text-gray-100">
                Initialize repository structure
              </span>
              <span className="block text-xs text-gray-500 dark:text-gray-400 mt-0.5">
                Creates .github/workflows, CLAUDE.md with guardrails, and PR template
              </span>
            </label>
          </div>

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
            {scaffolding ? "Scaffolding..." : loading ? "Starting..." : "Start Pipeline"}
          </button>
        </div>
      </div>
    </div>
  );
}
