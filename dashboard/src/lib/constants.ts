export const PIPELINE_STAGES = [
  { key: "triggered", label: "Triggered", color: "text-gray-400" },
  { key: "requirements_analyzed", label: "Requirements", color: "text-cyan-400" },
  { key: "epic_created", label: "Epic", color: "text-purple-400" },
  { key: "stories_created", label: "Stories", color: "text-blue-400" },
  { key: "plan_created", label: "Plan", color: "text-indigo-400" },
  { key: "code_implemented", label: "Code", color: "text-amber-400" },
  { key: "code_reviewed", label: "Review", color: "text-orange-400" },
  { key: "security_cleared", label: "Security", color: "text-rose-400" },
  { key: "build_monitoring", label: "Build", color: "text-yellow-400" },
  { key: "tests_complete", label: "Tests", color: "text-lime-400" },
  { key: "deploying", label: "Deploy", color: "text-emerald-400" },
  { key: "deployed", label: "Live", color: "text-green-400" },
  { key: "done", label: "Done", color: "text-green-500" },
  { key: "failed", label: "Failed", color: "text-red-400" },
] as const;

export const AGENT_INFO: Record<
  string,
  { label: string; icon: string; provider: string; color: string }
> = {
  document_analyzer: {
    label: "Document Analyzer",
    icon: "📄",
    provider: "Groq",
    color: "#0ea5e9",
  },
  tech_detector: {
    label: "Tech Detector",
    icon: "🔬",
    provider: "Groq",
    color: "#0284c7",
  },
  requirements_analyst: {
    label: "Requirements Analyst",
    icon: "📋",
    provider: "Groq",
    color: "#06b6d4",
  },
  product_director: {
    label: "Product Director",
    icon: "🎯",
    provider: "OpenAI",
    color: "#8b5cf6",
  },
  product_director_epic: {
    label: "Product Director (Epic)",
    icon: "🏔️",
    provider: "OpenAI",
    color: "#7c3aed",
  },
  product_director_stories: {
    label: "Product Director (Stories)",
    icon: "📝",
    provider: "OpenAI",
    color: "#a78bfa",
  },
  engineering_manager: {
    label: "Engineering Manager",
    icon: "🏗️",
    provider: "Claude",
    color: "#3b82f6",
  },
  senior_developer: {
    label: "Senior Developer",
    icon: "💻",
    provider: "Claude",
    color: "#f59e0b",
  },
  db_engineer: {
    label: "DB Engineer",
    icon: "🗄️",
    provider: "Claude",
    color: "#d946ef",
  },
  staff_reviewer: {
    label: "Staff Reviewer",
    icon: "🔍",
    provider: "Codex",
    color: "#f97316",
  },
  security_expert: {
    label: "Security Expert",
    icon: "🛡️",
    provider: "Claude",
    color: "#ef4444",
  },
  ci_monitor: {
    label: "CI Monitor",
    icon: "⚡",
    provider: "Groq",
    color: "#eab308",
  },
  qa_tester: {
    label: "QA Tester",
    icon: "🧪",
    provider: "Claude",
    color: "#22c55e",
  },
  infra_provisioner: {
    label: "Infra Provisioner",
    icon: "☁️",
    provider: "Claude",
    color: "#14b8a6",
  },
  release_manager: {
    label: "Release Manager",
    icon: "🚀",
    provider: "Groq",
    color: "#10b981",
  },
};

export const STAGE_TO_AGENT: Record<string, string> = {
  triggered: "document_analyzer",
  requirements_analyzed: "requirements_analyst",
  epic_created: "product_director",
  stories_created: "engineering_manager",
  plan_created: "senior_developer",
  code_implemented: "db_engineer",
  code_reviewed: "security_expert",
  security_cleared: "ci_monitor",
  build_monitoring: "qa_tester",
  tests_complete: "infra_provisioner",
  deploying: "release_manager",
};
