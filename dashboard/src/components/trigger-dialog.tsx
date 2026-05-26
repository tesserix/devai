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
    // Warm the repo profile in memory so the pipeline run launched
    // from this dialog already has tech-stack context. Fire-and-forget;
    // the run itself doesn't block on the scan.
    const [owner, name] = fullName.split("/", 2);
    if (owner && name) {
      fetch(
        `/api/scm/repos/${encodeURIComponent(owner)}/${encodeURIComponent(name)}/scan`,
        { method: "POST" }
      ).catch(() => undefined);
    }
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

  // All chrome reads from design tokens so the dialog adapts to the
  // active theme (light slate / dark zinc). No hard-coded grays or
  // indigo — that's how the previous version went black-on-black in
  // light mode.
  const inputStyle = {
    background: "var(--surface)",
    border: "1px solid var(--border)",
    color: "var(--ink-strong)",
  } as const;
  const subInputStyle = {
    background: "var(--surface-muted)",
    border: "1px solid var(--border-subtle)",
    color: "var(--ink-strong)",
  } as const;
  const inputBase =
    "w-full px-3 py-2.5 rounded-md text-sm outline-none focus:border-[var(--ink-strong)] focus:ring-1 focus:ring-[var(--accent-soft-bg-2)] transition-colors placeholder:opacity-60";
  const dropdownContainerStyle = {
    background: "var(--surface-raised)",
    border: "1px solid var(--border)",
    boxShadow: "var(--shadow-raised)",
  } as const;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center px-4"
      style={{ background: "var(--surface-overlay)", backdropFilter: "blur(4px)" }}
      onClick={(e) => e.target === e.currentTarget && onClose()}
    >
      <div
        className="rounded-xl w-full max-w-lg p-6 max-h-[90vh] overflow-y-auto"
        style={{
          background: "var(--surface-raised)",
          border: "1px solid var(--border)",
          boxShadow: "var(--shadow-raised)",
          color: "var(--ink)",
        }}
      >
        <div className="flex items-center justify-between mb-1">
          <h2 className="font-serif text-lg font-medium" style={{ color: "var(--ink-strong)" }}>
            New Pipeline Run
          </h2>
          <button
            onClick={onClose}
            className="w-7 h-7 flex items-center justify-center rounded-md transition-colors"
            style={{ color: "var(--ink-muted)" }}
            onMouseEnter={(e) => (e.currentTarget.style.background = "var(--surface-hover)")}
            onMouseLeave={(e) => (e.currentTarget.style.background = "transparent")}
            aria-label="Close"
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
              <line x1="18" y1="6" x2="6" y2="18" />
              <line x1="6" y1="6" x2="18" y2="18" />
            </svg>
          </button>
        </div>
        <p className="text-sm mb-5" style={{ color: "var(--ink-soft)" }}>
          Select a repository, link a project board, and describe your requirements.
        </p>

        <div className="space-y-4">
          {/* Repository selector */}
          <div ref={dropdownRef} className="relative">
            <label className="block text-sm font-medium mb-1.5" style={{ color: "var(--ink-strong)" }}>
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
              className={inputBase}
              style={inputStyle}
            />

            {dropdownOpen && (
              <div
                className="absolute z-10 w-full mt-1 rounded-md max-h-56 overflow-y-auto"
                style={dropdownContainerStyle}
              >
                {/* Pinned to the top so the create action stays reachable
                    no matter how many repos are in the list. */}
                <button
                  onClick={() => {
                    setShowCreate(true);
                    setDropdownOpen(false);
                  }}
                  className="w-full text-left px-3 py-2.5 text-sm font-medium transition-colors sticky top-0 z-10"
                  style={{
                    color: "var(--accent)",
                    background: "var(--surface-raised)",
                    borderBottom: "1px solid var(--border-subtle)",
                  }}
                  onMouseEnter={(e) => (e.currentTarget.style.background = "var(--accent-soft-bg)")}
                  onMouseLeave={(e) => (e.currentTarget.style.background = "var(--surface-raised)")}
                >
                  + Create new repository
                </button>
                {filteredRepos.length > 0 ? (
                  filteredRepos.map((r) => (
                    <button
                      key={r.full_name}
                      onClick={() => handleSelectRepo(r.full_name)}
                      className="w-full text-left px-3 py-2.5 transition-colors"
                      style={{ borderBottom: "1px solid var(--border-subtle)" }}
                      onMouseEnter={(e) => (e.currentTarget.style.background = "var(--surface-hover)")}
                      onMouseLeave={(e) => (e.currentTarget.style.background = "transparent")}
                    >
                      <div className="flex items-center justify-between">
                        <span className="text-sm font-medium font-mono" style={{ color: "var(--ink-strong)" }}>
                          {r.full_name}
                        </span>
                        <div className="flex items-center gap-2">
                          {r.language && (
                            <span className="text-xs" style={{ color: "var(--ink-muted)" }}>
                              {r.language}
                            </span>
                          )}
                          {r.private && (
                            <span
                              className="text-[10px] px-1.5 py-0.5 rounded font-mono"
                              style={{
                                background: "var(--surface-muted)",
                                color: "var(--ink-soft)",
                                border: "1px solid var(--border-subtle)",
                              }}
                            >
                              private
                            </span>
                          )}
                        </div>
                      </div>
                      {r.description && (
                        <p className="text-xs mt-0.5 truncate" style={{ color: "var(--ink-soft)" }}>
                          {r.description}
                        </p>
                      )}
                    </button>
                  ))
                ) : (
                  <div className="px-3 py-3 text-sm" style={{ color: "var(--ink-muted)" }}>
                    {loadingRepos ? "Loading..." : "No repositories found"}
                  </div>
                )}
              </div>
            )}
          </div>

          {/* Create repo inline form */}
          {showCreate && (
            <div
              className="p-4 rounded-md space-y-3"
              style={{
                background: "var(--accent-soft-bg)",
                border: "1px solid var(--accent-soft-bd)",
              }}
            >
              <div className="flex items-center justify-between">
                <span className="text-sm font-medium" style={{ color: "var(--ink-strong)" }}>
                  Create Repository
                </span>
                <button
                  onClick={() => setShowCreate(false)}
                  className="text-xs transition-colors"
                  style={{ color: "var(--ink-muted)" }}
                  onMouseEnter={(e) => (e.currentTarget.style.color = "var(--ink)")}
                  onMouseLeave={(e) => (e.currentTarget.style.color = "var(--ink-muted)")}
                >
                  Cancel
                </button>
              </div>
              <div>
                <label className="block text-xs font-medium mb-1" style={{ color: "var(--ink-soft)" }}>
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
                    className="w-full px-3 py-2 rounded-md text-sm outline-none transition-colors placeholder:opacity-60"
                    style={{
                      background: "var(--surface)",
                      color: "var(--ink-strong)",
                      border: `1px solid ${
                        repoNameStatus === "taken"
                          ? "var(--error)"
                          : repoNameStatus === "available"
                            ? "var(--ok)"
                            : "var(--border)"
                      }`,
                    }}
                  />
                  {repoNameStatus === "checking" && (
                    <span className="absolute right-3 top-2.5 text-xs" style={{ color: "var(--ink-muted)" }}>
                      checking…
                    </span>
                  )}
                  {repoNameStatus === "available" && (
                    <span className="absolute right-3 top-2.5 text-xs" style={{ color: "var(--ok)" }}>
                      available
                    </span>
                  )}
                  {repoNameStatus === "taken" && (
                    <span className="absolute right-3 top-2.5 text-xs" style={{ color: "var(--error)" }}>
                      already exists
                    </span>
                  )}
                </div>
              </div>
              <div>
                <label className="block text-xs font-medium mb-1" style={{ color: "var(--ink-soft)" }}>
                  Description (optional)
                </label>
                <input
                  type="text"
                  placeholder="Brief description"
                  value={newRepoDesc}
                  onChange={(e) => setNewRepoDesc(e.target.value)}
                  className="w-full px-3 py-2 rounded-md text-sm outline-none transition-colors placeholder:opacity-60"
                  style={inputStyle}
                />
              </div>
              <button
                onClick={handleCreateRepo}
                disabled={creating || !newRepoName.trim() || repoNameStatus === "taken" || repoNameStatus === "checking"}
                className="px-4 py-2 text-sm font-medium rounded-md disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                style={{ background: "var(--primary)", color: "var(--primary-ink)" }}
              >
                {creating ? "Creating…" : "Create"}
              </button>
            </div>
          )}

          {/* Project selector */}
          <div ref={projectDropdownRef} className="relative">
            <label
              className="block text-sm font-medium mb-1.5 flex items-baseline gap-1.5"
              style={{ color: "var(--ink-strong)" }}
            >
              Project Board
              <span className="text-xs font-normal" style={{ color: "var(--ink-muted)" }}>
                (optional)
              </span>
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
              className={inputBase}
              style={inputStyle}
            />

            {projectDropdownOpen && (
              <div
                className="absolute z-10 w-full mt-1 rounded-md max-h-56 overflow-y-auto"
                style={dropdownContainerStyle}
              >
                <button
                  onClick={() => {
                    setShowCreateProject(true);
                    setProjectDropdownOpen(false);
                  }}
                  className="w-full text-left px-3 py-2.5 text-sm font-medium transition-colors sticky top-0 z-10"
                  style={{
                    color: "var(--accent)",
                    background: "var(--surface-raised)",
                    borderBottom: "1px solid var(--border-subtle)",
                  }}
                  onMouseEnter={(e) => (e.currentTarget.style.background = "var(--accent-soft-bg)")}
                  onMouseLeave={(e) => (e.currentTarget.style.background = "var(--surface-raised)")}
                >
                  + Create new project board
                </button>
                {filteredProjects.length > 0 ? (
                  filteredProjects.map((p) => (
                    <button
                      key={p.id}
                      onClick={() => handleSelectProject(p)}
                      className="w-full text-left px-3 py-2.5 transition-colors"
                      style={{ borderBottom: "1px solid var(--border-subtle)" }}
                      onMouseEnter={(e) => (e.currentTarget.style.background = "var(--surface-hover)")}
                      onMouseLeave={(e) => (e.currentTarget.style.background = "transparent")}
                    >
                      <div className="flex items-center justify-between">
                        <span className="text-sm font-medium" style={{ color: "var(--ink-strong)" }}>
                          {p.title}
                        </span>
                        <span className="text-xs font-mono" style={{ color: "var(--ink-muted)" }}>
                          #{p.number}
                        </span>
                      </div>
                      {p.description && (
                        <p className="text-xs mt-0.5 truncate" style={{ color: "var(--ink-soft)" }}>
                          {p.description}
                        </p>
                      )}
                    </button>
                  ))
                ) : (
                  <div className="px-3 py-3 text-sm" style={{ color: "var(--ink-muted)" }}>
                    {loadingProjects ? "Loading..." : "No projects found"}
                  </div>
                )}
              </div>
            )}
          </div>

          {/* Create project inline form */}
          {showCreateProject && (
            <div
              className="p-4 rounded-md space-y-3"
              style={{
                background: "var(--accent-soft-bg)",
                border: "1px solid var(--accent-soft-bd)",
              }}
            >
              <div className="flex items-center justify-between">
                <span className="text-sm font-medium" style={{ color: "var(--ink-strong)" }}>
                  Create Project Board
                </span>
                <button
                  onClick={() => setShowCreateProject(false)}
                  className="text-xs transition-colors"
                  style={{ color: "var(--ink-muted)" }}
                  onMouseEnter={(e) => (e.currentTarget.style.color = "var(--ink)")}
                  onMouseLeave={(e) => (e.currentTarget.style.color = "var(--ink-muted)")}
                >
                  Cancel
                </button>
              </div>
              <div>
                <label className="block text-xs font-medium mb-1" style={{ color: "var(--ink-soft)" }}>
                  Project Title
                </label>
                <input
                  type="text"
                  placeholder="Sprint 1 — Feature X"
                  value={newProjectName}
                  onChange={(e) => setNewProjectName(e.target.value)}
                  className="w-full px-3 py-2 rounded-md text-sm outline-none transition-colors placeholder:opacity-60"
                  style={inputStyle}
                />
              </div>
              <div>
                <label className="block text-xs font-medium mb-1" style={{ color: "var(--ink-soft)" }}>
                  Description (optional)
                </label>
                <input
                  type="text"
                  placeholder="What is this project about?"
                  value={newProjectDesc}
                  onChange={(e) => setNewProjectDesc(e.target.value)}
                  className="w-full px-3 py-2 rounded-md text-sm outline-none transition-colors placeholder:opacity-60"
                  style={inputStyle}
                />
              </div>
              <button
                onClick={handleCreateProject}
                disabled={creatingProject || !newProjectName.trim()}
                className="px-4 py-2 text-sm font-medium rounded-md disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                style={{ background: "var(--primary)", color: "var(--primary-ink)" }}
              >
                {creatingProject ? "Creating…" : "Create Project"}
              </button>
            </div>
          )}

          {/* Scaffold toggle */}
          <div
            className="flex items-start gap-3 p-3 rounded-md"
            style={{
              background: "var(--surface-muted)",
              border: "1px solid var(--border-subtle)",
            }}
          >
            <input
              type="checkbox"
              id="scaffold"
              checked={scaffold}
              onChange={(e) => setScaffold(e.target.checked)}
              className="mt-0.5 h-4 w-4 rounded cursor-pointer"
              style={{ accentColor: "var(--accent)" }}
            />
            <label htmlFor="scaffold" className="cursor-pointer">
              <span className="block text-sm font-medium" style={{ color: "var(--ink-strong)" }}>
                Initialize repository structure
              </span>
              <span className="block text-xs mt-0.5" style={{ color: "var(--ink-soft)" }}>
                Creates .github/workflows, CLAUDE.md guardrails, and PR template.
              </span>
            </label>
          </div>

          {/* Requirements */}
          <div>
            <label className="block text-sm font-medium mb-1.5" style={{ color: "var(--ink-strong)" }}>
              Requirements
            </label>
            <textarea
              rows={5}
              placeholder="Describe the feature or requirements…"
              value={requirements}
              onChange={(e) => setRequirements(e.target.value)}
              className="w-full px-3 py-2.5 rounded-md text-sm outline-none resize-none transition-colors placeholder:opacity-60"
              style={subInputStyle}
            />
          </div>
        </div>

        <div className="mt-5 flex justify-end gap-2">
          <button
            onClick={onClose}
            className="px-4 py-2 text-sm font-medium rounded-md transition-colors"
            style={{
              color: "var(--ink)",
              background: "var(--surface)",
              border: "1px solid var(--border)",
            }}
            onMouseEnter={(e) => (e.currentTarget.style.background = "var(--surface-hover)")}
            onMouseLeave={(e) => (e.currentTarget.style.background = "var(--surface)")}
          >
            Cancel
          </button>
          <button
            onClick={handleSubmit}
            disabled={loading || !selectedRepo || !requirements}
            className="px-4 py-2 text-sm font-medium rounded-md disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            style={{ background: "var(--primary)", color: "var(--primary-ink)" }}
          >
            {scaffolding ? "Scaffolding…" : loading ? "Starting…" : "Start Pipeline"}
          </button>
        </div>
      </div>
    </div>
  );
}
