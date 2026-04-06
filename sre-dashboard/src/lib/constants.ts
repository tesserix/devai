export const SRE_AGENTS: Record<
  string,
  { label: string; icon: string; provider: string; color: string }
> = {
  discovery: {
    label: "Discovery Agent",
    icon: "🔭",
    provider: "Groq",
    color: "#06b6d4",
  },
  infra_monitor: {
    label: "Infra Monitor",
    icon: "🖥️",
    provider: "Groq",
    color: "#3b82f6",
  },
  log_analyzer: {
    label: "Log Analyzer",
    icon: "📜",
    provider: "Groq",
    color: "#f59e0b",
  },
  perf_monitor: {
    label: "Perf Monitor",
    icon: "⚡",
    provider: "Groq + Prometheus",
    color: "#ef4444",
  },
  cost_analyzer: {
    label: "Cost Analyzer",
    icon: "💰",
    provider: "Groq",
    color: "#22c55e",
  },
  capacity_planner: {
    label: "Capacity Planner",
    icon: "📊",
    provider: "Groq",
    color: "#8b5cf6",
  },
  incident_responder: {
    label: "Incident Responder",
    icon: "🚨",
    provider: "Claude",
    color: "#dc2626",
  },
};

export const SEVERITY_STYLES: Record<string, { bg: string; text: string; dot: string }> = {
  critical: { bg: "bg-red-500/10", text: "text-red-400", dot: "bg-red-500" },
  high: { bg: "bg-orange-500/10", text: "text-orange-400", dot: "bg-orange-500" },
  medium: { bg: "bg-amber-500/10", text: "text-amber-400", dot: "bg-amber-500" },
  low: { bg: "bg-blue-500/10", text: "text-blue-400", dot: "bg-blue-400" },
  info: { bg: "bg-gray-500/10", text: "text-gray-400", dot: "bg-gray-400" },
};
