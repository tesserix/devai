// NOTE: the pipeline flow is no longer hardcoded here. PipelineFlow renders
// from the blueprint-graph endpoint (GET /api/pipeline/blueprints/{name}), so
// the blueprint YAML is the single source of truth and a new/UI-authored
// blueprint shows up with no UI change. The former PIPELINE_STAGES and
// STAGE_TO_AGENT constants were removed; AGENT_INFO below stays — it's the
// agent-metadata catalog used by agent-card / a2a-feed / hierarchy views.

export const AGENT_INFO: Record<
  string,
  { label: string; icon: string; provider: string; color: string; role: "coordinator" | "specialist" }
> = {
  supervisor: {
    label: "Supervisor",
    icon: "S",
    provider: "OpenAI",
    color: "#4f46e5",
    role: "coordinator",
  },
  orchestrator: {
    label: "Orchestrator",
    icon: "O",
    provider: "OpenAI",
    color: "#4338ca",
    role: "coordinator",
  },
  document_analyzer: {
    label: "Document Analyzer",
    icon: "D",
    provider: "Gemini",
    color: "#0369a1",
    role: "specialist",
  },
  tech_detector: {
    label: "Tech Detector",
    icon: "T",
    provider: "Gemini",
    color: "#075985",
    role: "specialist",
  },
  requirements_analyst: {
    label: "Requirements Analyst",
    icon: "R",
    provider: "OpenAI",
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
    provider: "OpenAI",
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
    provider: "Claude",
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
