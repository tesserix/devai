export const PIPELINE_STAGES = [
  { key: "triggered", label: "Triggered", color: "text-gray-400" },
  { key: "plan_approved", label: "Supervisor", color: "text-violet-400" },
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
  { label: string; icon: string; provider: string; color: string; role: "coordinator" | "specialist" }
> = {
  supervisor: {
    label: "Supervisor",
    icon: "🧠",
    provider: "Claude",
    color: "#a855f7",
    role: "coordinator",
  },
  orchestrator: {
    label: "Orchestrator",
    icon: "🎛️",
    provider: "Claude",
    color: "#6366f1",
    role: "coordinator",
  },
  document_analyzer: {
    label: "Document Analyzer",
    icon: "📄",
    provider: "Groq",
    color: "#0ea5e9",
    role: "specialist",
  },
  tech_detector: {
    label: "Tech Detector",
    icon: "🔬",
    provider: "Groq",
    color: "#0284c7",
    role: "specialist",
  },
  requirements_analyst: {
    label: "Requirements Analyst",
    icon: "📋",
    provider: "Groq",
    color: "#06b6d4",
    role: "specialist",
  },
  product_director: {
    label: "Product Director",
    icon: "🎯",
    provider: "OpenAI",
    color: "#8b5cf6",
    role: "specialist",
  },
  product_director_epic: {
    label: "Product Director (Epic)",
    icon: "🏔️",
    provider: "OpenAI",
    color: "#7c3aed",
    role: "specialist",
  },
  product_director_stories: {
    label: "Product Director (Stories)",
    icon: "📝",
    provider: "OpenAI",
    color: "#a78bfa",
    role: "specialist",
  },
  engineering_manager: {
    label: "Engineering Manager",
    icon: "🏗️",
    provider: "Claude",
    color: "#3b82f6",
    role: "specialist",
  },
  senior_developer: {
    label: "Senior Developer",
    icon: "💻",
    provider: "Claude",
    color: "#f59e0b",
    role: "specialist",
  },
  db_engineer: {
    label: "DB Engineer",
    icon: "🗄️",
    provider: "Claude",
    color: "#d946ef",
    role: "specialist",
  },
  staff_reviewer: {
    label: "Staff Reviewer",
    icon: "🔍",
    provider: "Codex",
    color: "#f97316",
    role: "specialist",
  },
  security_expert: {
    label: "Security Expert",
    icon: "🛡️",
    provider: "Claude",
    color: "#ef4444",
    role: "specialist",
  },
  ci_monitor: {
    label: "CI Monitor",
    icon: "⚡",
    provider: "Groq",
    color: "#eab308",
    role: "specialist",
  },
  qa_tester: {
    label: "QA Tester",
    icon: "🧪",
    provider: "Claude",
    color: "#22c55e",
    role: "specialist",
  },
  infra_provisioner: {
    label: "Infra Provisioner",
    icon: "☁️",
    provider: "Claude",
    color: "#14b8a6",
    role: "specialist",
  },
  release_manager: {
    label: "Release Manager",
    icon: "🚀",
    provider: "Groq",
    color: "#10b981",
    role: "specialist",
  },
};

export const STAGE_TO_AGENT: Record<string, string> = {
  triggered: "supervisor",
  plan_approved: "orchestrator",
  requirements_analyzed: "requirements_analyst",
  epic_created: "product_director",
  stories_created: "engineering_manager",
  plan_created: "senior_developer",
  code_implemented: "db_engineer",
  db_migrated: "staff_reviewer",
  code_reviewed: "orchestrator",
  security_cleared: "orchestrator",
  build_monitoring: "qa_tester",
  tests_complete: "orchestrator",
  deploying: "release_manager",
  deployed: "orchestrator",
};

/** Agent hierarchy for the delegation visualization */
export const AGENT_HIERARCHY = {
  supervisor: {
    label: "Supervisor Agent",
    description: "Plans architecture & delegates tasks",
    children: ["orchestrator"],
  },
  orchestrator: {
    label: "Orchestrator Agent",
    description: "Manages execution workflow",
    children: [
      "document_analyzer",
      "tech_detector",
      "requirements_analyst",
      "product_director",
      "engineering_manager",
      "senior_developer",
      "db_engineer",
      "staff_reviewer",
      "security_expert",
      "ci_monitor",
      "qa_tester",
      "infra_provisioner",
      "release_manager",
    ],
  },
} as const;

/** Execution phases defined by the Supervisor */
export const EXECUTION_PHASES = [
  {
    name: "Analysis",
    agents: ["document_analyzer", "tech_detector", "requirements_analyst"],
    color: "#06b6d4",
  },
  {
    name: "Planning",
    agents: ["product_director", "engineering_manager"],
    color: "#8b5cf6",
  },
  {
    name: "Implementation",
    agents: ["senior_developer", "db_engineer"],
    color: "#f59e0b",
  },
  {
    name: "Quality",
    agents: ["staff_reviewer", "security_expert", "ci_monitor", "qa_tester"],
    color: "#ef4444",
  },
  {
    name: "Deployment",
    agents: ["infra_provisioner", "release_manager"],
    color: "#10b981",
  },
] as const;
