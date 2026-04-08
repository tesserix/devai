export const SRE_AGENTS: Record<
  string,
  { label: string; provider: string; colorClass: string }
> = {
  discovery: {
    label: "Discovery Agent",
    provider: "Groq",
    colorClass: "text-cyan-600 dark:text-cyan-400",
  },
  infra_monitor: {
    label: "Infra Monitor",
    provider: "Groq",
    colorClass: "text-blue-600 dark:text-blue-400",
  },
  log_analyzer: {
    label: "Log Analyzer",
    provider: "Groq",
    colorClass: "text-amber-600 dark:text-amber-400",
  },
  perf_monitor: {
    label: "Perf Monitor",
    provider: "Groq + Prometheus",
    colorClass: "text-red-600 dark:text-red-400",
  },
  cost_analyzer: {
    label: "Cost Analyzer",
    provider: "Groq",
    colorClass: "text-emerald-600 dark:text-emerald-400",
  },
  capacity_planner: {
    label: "Capacity Planner",
    provider: "Groq",
    colorClass: "text-violet-600 dark:text-violet-400",
  },
  incident_responder: {
    label: "Incident Responder",
    provider: "Claude",
    colorClass: "text-red-600 dark:text-red-400",
  },
};

export const SEVERITY_STYLES: Record<string, { border: string; bg: string; text: string; dot: string }> = {
  critical: {
    border: "border-red-200 dark:border-red-500/30",
    bg: "bg-red-50 dark:bg-red-500/10",
    text: "text-red-600 dark:text-red-400",
    dot: "bg-red-500",
  },
  high: {
    border: "border-orange-200 dark:border-orange-500/30",
    bg: "bg-orange-50 dark:bg-orange-500/10",
    text: "text-orange-600 dark:text-orange-400",
    dot: "bg-orange-500",
  },
  medium: {
    border: "border-amber-200 dark:border-amber-500/30",
    bg: "bg-amber-50 dark:bg-amber-500/10",
    text: "text-amber-600 dark:text-amber-400",
    dot: "bg-amber-500",
  },
  low: {
    border: "border-blue-200 dark:border-blue-500/30",
    bg: "bg-blue-50 dark:bg-blue-500/10",
    text: "text-blue-600 dark:text-blue-400",
    dot: "bg-blue-400",
  },
  info: {
    border: "border-gray-200 dark:border-slate-700",
    bg: "bg-gray-50 dark:bg-slate-800/50",
    text: "text-gray-500 dark:text-slate-400",
    dot: "bg-gray-400",
  },
};
