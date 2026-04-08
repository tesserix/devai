export const PIPELINE_STAGES = [
  { key: "triggered", label: "Triggered", color: "text-gray-500" },
  { key: "plan_approved", label: "Supervisor", color: "text-indigo-600" },
  { key: "requirements_analyzed", label: "Requirements", color: "text-slate-600" },
  { key: "epic_created", label: "Epic", color: "text-indigo-600" },
  { key: "stories_created", label: "Stories", color: "text-blue-600" },
  { key: "plan_created", label: "Plan", color: "text-indigo-600" },
  { key: "code_implemented", label: "Code", color: "text-amber-600" },
  { key: "code_reviewed", label: "Review", color: "text-orange-600" },
  { key: "security_cleared", label: "Security", color: "text-red-600" },
  { key: "build_monitoring", label: "Build", color: "text-yellow-600" },
  { key: "tests_complete", label: "Tests", color: "text-green-600" },
  { key: "deploying", label: "Deploy", color: "text-teal-600" },
  { key: "deployed", label: "Live", color: "text-green-700" },
  { key: "done", label: "Done", color: "text-green-700" },
  { key: "failed", label: "Failed", color: "text-red-600" },
] as const;

export const AGENT_INFO: Record<
  string,
  { label: string; icon: string; provider: string; color: string; role: "coordinator" | "specialist" }
> = {
  supervisor: {
    label: "Supervisor",
    icon: "S",
    provider: "Claude",
    color: "#4f46e5",
    role: "coordinator",
  },
  orchestrator: {
    label: "Orchestrator",
    icon: "O",
    provider: "Claude",
    color: "#4338ca",
    role: "coordinator",
  },
  document_analyzer: {
    label: "Document Analyzer",
    icon: "D",
    provider: "Groq",
    color: "#0369a1",
    role: "specialist",
  },
  tech_detector: {
    label: "Tech Detector",
    icon: "T",
    provider: "Groq",
    color: "#075985",
    role: "specialist",
  },
  requirements_analyst: {
    label: "Requirements Analyst",
    icon: "R",
    provider: "Groq",
    color: "#0e7490",
    role: "specialist",
  },
  product_director: {
    label: "Product Director",
    icon: "P",
    provider: "OpenAI",
    color: "#6d28d9",
    role: "specialist",
  },
  product_director_epic: {
    label: "Product Director (Epic)",
    icon: "E",
    provider: "OpenAI",
    color: "#5b21b6",
    role: "specialist",
  },
  product_director_stories: {
    label: "Product Director (Stories)",
    icon: "S",
    provider: "OpenAI",
    color: "#7c3aed",
    role: "specialist",
  },
  engineering_manager: {
    label: "Engineering Manager",
    icon: "E",
    provider: "Claude",
    color: "#1d4ed8",
    role: "specialist",
  },
  senior_developer: {
    label: "Senior Developer",
    icon: "D",
    provider: "Claude",
    color: "#b45309",
    role: "specialist",
  },
  db_engineer: {
    label: "DB Engineer",
    icon: "B",
    provider: "Claude",
    color: "#7e22ce",
    role: "specialist",
  },
  staff_reviewer: {
    label: "Staff Reviewer",
    icon: "R",
    provider: "Codex",
    color: "#c2410c",
    role: "specialist",
  },
  security_expert: {
    label: "Security Expert",
    icon: "S",
    provider: "Claude",
    color: "#b91c1c",
    role: "specialist",
  },
  ci_monitor: {
    label: "CI Monitor",
    icon: "C",
    provider: "Groq",
    color: "#a16207",
    role: "specialist",
  },
  qa_tester: {
    label: "QA Tester",
    icon: "Q",
    provider: "Claude",
    color: "#15803d",
    role: "specialist",
  },
  infra_provisioner: {
    label: "Infra Provisioner",
    icon: "I",
    provider: "Claude",
    color: "#0f766e",
    role: "specialist",
  },
  release_manager: {
    label: "Release Manager",
    icon: "R",
    provider: "Groq",
    color: "#047857",
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
    color: "#0e7490",
  },
  {
    name: "Planning",
    agents: ["product_director", "engineering_manager"],
    color: "#4f46e5",
  },
  {
    name: "Implementation",
    agents: ["senior_developer", "db_engineer"],
    color: "#b45309",
  },
  {
    name: "Quality",
    agents: ["staff_reviewer", "security_expert", "ci_monitor", "qa_tester"],
    color: "#b91c1c",
  },
  {
    name: "Deployment",
    agents: ["infra_provisioner", "release_manager"],
    color: "#047857",
  },
] as const;
