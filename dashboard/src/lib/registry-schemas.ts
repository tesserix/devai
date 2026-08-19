// Per-kind form schemas + starter manifests + manifest linting — ported from
// the Agentic Registry (agentic-registry/web/src/lib/schemas.ts) so DevAI's
// authoring editor produces the EXACT same registry CR shape and validation.
// Each artifact is a Kubernetes-style object (apiVersion/kind/metadata/spec);
// only the `spec` shape differs per kind.

export const API_VERSION = "registry.agentic.dev/v1alpha1";

export type FieldType =
  | "text"
  | "textarea"
  | "number"
  | "select"
  | "checkbox"
  | "ref" // one catalog artifact name (itemKind = the plural)
  | "refList" // multi-select of catalog artifact names (itemKind = the plural)
  | "group" // a nested object rendered from `children`
  | "objectList"; // a list of records, each rendered from `children`

export interface Field {
  path: string; // dot-path into the doc, e.g. "spec.title"
  label: string;
  type: FieldType;
  placeholder?: string;
  help?: string;
  options?: string[];
  required?: boolean;
  mono?: boolean;
  min?: number;
  max?: number;
  step?: number;
  itemKind?: string; // for "refList": the plural to query, e.g. "skills"
  children?: Field[]; // for "group" / "objectList": nested field shapes (paths are RELATIVE)
}

// Common metadata fields shared by every kind.
const META: Field[] = [
  { path: "metadata.name", label: "Name", type: "text", placeholder: "my-artifact", required: true, mono: true, help: "Unique identifier within the collection." },
  { path: "metadata.tag", label: "Version", type: "text", placeholder: "auto · v0.0.1", mono: true, help: "Leave blank to auto-increment (v0.0.1, v0.0.2, …); set one (e.g. v1.2.0) to pin." },
  {
    path: "metadata.visibility",
    label: "Visibility",
    type: "select",
    options: ["private"],
    help: "User-authored artifacts stay private to your account while tenant sharing is being hardened.",
  },
];

const AGENT_MODEL_PROVIDER_OPTIONS = ["anthropic", "openai", "google", "vertex_gemini", "groq"];
const AGENT_RISK_LEVEL_OPTIONS = ["low", "medium", "high", "critical"];
const AGENT_MODEL_PROVIDERS = new Set([...AGENT_MODEL_PROVIDER_OPTIONS, "claude", "gemini", "vertex"]);
const AGENT_RISK_LEVELS = new Set(AGENT_RISK_LEVEL_OPTIONS);

// Spec fields per kind.
const SPEC: Record<string, Field[]> = {
  Skill: [
    { path: "spec.title", label: "Title", type: "text", placeholder: "Senior Go Engineer", required: true },
    { path: "spec.description", label: "Description", type: "textarea", placeholder: "What this skill does and when to use it." },
    { path: "spec.source.repository", label: "Source repository", type: "text", placeholder: "https://github.com/org/repo", mono: true },
  ],
  Tool: [
    { path: "spec.title", label: "Title", type: "text", placeholder: "Postgres Query", required: true },
    { path: "spec.description", label: "Description", type: "textarea", placeholder: "What the tool does and its inputs/outputs." },
  ],
  MCPServer: [
    { path: "spec.name", label: "Server name", type: "text", placeholder: "io.github.acme/files", required: true, mono: true },
    { path: "spec.version", label: "Version", type: "text", placeholder: "1.0.0", mono: true },
    { path: "spec.description", label: "Description", type: "textarea", placeholder: "What this MCP server exposes." },
    { path: "spec.packages.0.registryType", label: "Package registry", type: "select", options: ["npm", "pypi", "oci", "nuget"] },
    { path: "spec.packages.0.identifier", label: "Package identifier", type: "text", placeholder: "@acme/files-mcp", mono: true },
    { path: "spec.packages.0.transport.type", label: "Transport", type: "select", options: ["stdio", "sse", "streamableHttp"] },
  ],
  Prompt: [
    { path: "spec.title", label: "Title", type: "text", placeholder: "Summarize Incident", required: true },
    { path: "spec.description", label: "Description", type: "textarea", placeholder: "What this prompt produces." },
    { path: "spec.template", label: "Template", type: "textarea", placeholder: "Summarize the following:\n\n{{input}}", mono: true, help: "Use {{variables}} for runtime values." },
  ],
  Dataset: [
    { path: "spec.description", label: "Description", type: "textarea", placeholder: "What behavior this immutable dataset version verifies." },
    {
      path: "spec.cases",
      label: "Cases",
      type: "objectList",
      required: true,
      help: "Add the core case fields here. Expected tools, forbidden tools, context, and tags can be added in the live manifest.",
      children: [
        { path: "id", label: "Case ID", type: "text", required: true, mono: true },
        { path: "input", label: "Input", type: "textarea", required: true },
        { path: "expectedOutput", label: "Expected output", type: "textarea" },
      ],
    },
  ],
  EvalSuite: [
    { path: "spec.description", label: "Description", type: "textarea", placeholder: "What this release gate verifies." },
    {
      path: "spec.datasetRef",
      label: "Pinned dataset",
      type: "group",
      required: true,
      help: "Both the dataset name and immutable version are required.",
      children: [
        { path: "ref", label: "Dataset", type: "text", required: true, mono: true },
        { path: "version", label: "Version", type: "text", required: true, mono: true },
      ],
    },
    {
      path: "spec.thresholds",
      label: "Thresholds",
      type: "group",
      children: [
        { path: "success", label: "Success rate", type: "number", min: 0, max: 1, step: 0.01 },
        { path: "safety", label: "Safety rate", type: "number", min: 0, max: 1, step: 0.01 },
        { path: "hallucination", label: "Hallucination rate", type: "number", min: 0, max: 1, step: 0.01 },
        { path: "p95_latency_s", label: "P95 latency (s)", type: "number", min: 0.1, step: 0.1 },
        { path: "cost_per_run_usd", label: "Cost / run (USD)", type: "number", min: 0, step: 0.001 },
      ],
    },
  ],
  Workflow: [
    { path: "spec.title", label: "Title", type: "text", placeholder: "Release Train", required: true },
    { path: "spec.description", label: "Description", type: "textarea", placeholder: "What this workflow orchestrates." },
  ],
  Blueprint: [
    { path: "spec.title", label: "Title", type: "text", placeholder: "RAG Chatbot", required: true },
    { path: "spec.description", label: "Description", type: "textarea", placeholder: "The pattern this blueprint wires together." },
  ],
  Agent: [
    { path: "spec.title", label: "Title", type: "text", placeholder: "On-Call Responder", required: true },
    { path: "spec.description", label: "Description", type: "textarea", placeholder: "What the agent does autonomously." },
    {
      path: "spec.model", label: "Model", type: "group", help: "The LLM this agent reasons with.", children: [
        { path: "provider", label: "Provider", type: "select", options: ["", ...AGENT_MODEL_PROVIDER_OPTIONS], required: true },
        { path: "name", label: "Model name", type: "text", placeholder: "claude-sonnet-4", mono: true, required: true },
        { path: "temperature", label: "Temperature", type: "number", placeholder: "0.3", mono: true, min: 0, max: 2, step: 0.1, help: "0–2." },
      ],
    },
    {
      path: "spec.promptRef",
      label: "Prompt reference",
      type: "ref",
      itemKind: "prompts",
      placeholder: "incident-responder-prompt-v1",
      help: "Choose a registry Prompt, or provide an inline system prompt below. One is required.",
    },
    {
      path: "spec.evalSuite",
      label: "Evaluation suite",
      type: "group",
      help: "Pin the immutable suite version that must pass before publication.",
      children: [
        { path: "ref", label: "Suite", type: "text", mono: true, required: true },
        { path: "version", label: "Version", type: "text", mono: true, required: true },
      ],
    },
    { path: "spec.systemPrompt", label: "System prompt", type: "textarea", placeholder: "You are an on-call SRE. …", help: "The agent's base instruction. References to Prompts below are available at runtime." },
    { path: "spec.skills", label: "Skills", type: "refList", itemKind: "skills", help: "Registry Skills this agent can perform." },
    { path: "spec.tools", label: "Tools", type: "refList", itemKind: "tools", help: "Registry Tools the agent may call." },
    { path: "spec.mcpServers", label: "MCP servers", type: "refList", itemKind: "mcp-servers", help: "MCP servers the agent connects to for tools." },
    { path: "spec.prompts", label: "Prompts", type: "refList", itemKind: "prompts", help: "Reusable Prompts available to the agent." },
    {
      path: "spec.limits", label: "Execution limits", type: "group", help: "Hard bounds applied to each agent run.", children: [
        { path: "maxTurns", label: "Maximum turns", type: "number", min: 1, max: 1000, step: 1, required: true },
        { path: "timeoutSeconds", label: "Timeout (seconds)", type: "number", min: 1, max: 86400, step: 1, required: true },
      ],
    },
    {
      path: "spec.riskLevel",
      label: "Risk level",
      type: "select",
      options: AGENT_RISK_LEVEL_OPTIONS,
      help: "High and critical agents require a human approval gate before consequential actions.",
    },
    {
      path: "spec.a2a", label: "A2A (advanced)", type: "group", help: "Agent-to-agent protocol config (optional).", children: [
        { path: "url", label: "Service URL", type: "text", placeholder: "https://…", mono: true, help: "Where consumers reach this agent over A2A." },
        { path: "preferredTransport", label: "Transport", type: "select", options: ["", "JSONRPC", "GRPC"] },
      ],
    },
  ],
};

export function fieldsFor(kind: string): Field[] {
  return [...META, ...(SPEC[kind] ?? SPEC.Skill)];
}

// starter returns a fresh, minimal doc for a kind with sensible defaults so the
// editor and the live manifest are never empty.
export function starter(kind: string): Record<string, unknown> {
  const meta = { name: "", tag: "", visibility: "private", labels: {} as Record<string, string> };
  const specByKind: Record<string, Record<string, unknown>> = {
    Skill: { title: "", description: "", source: { repository: "" } },
    Tool: { title: "", description: "" },
    MCPServer: {
      name: "",
      version: "1.0.0",
      description: "",
      packages: [{ registryType: "npm", identifier: "", transport: { type: "stdio" } }],
    },
    Prompt: { title: "", description: "", template: "" },
    Dataset: { description: "", cases: [] },
    EvalSuite: {
      description: "",
      datasetRef: { ref: "", version: "" },
      scorers: [],
      thresholds: {
        success: null,
        safety: null,
        hallucination: null,
        p95_latency_s: null,
        cost_per_run_usd: null,
      },
    },
    Workflow: { title: "", description: "", nodes: [], edges: [] },
    Blueprint: { title: "", description: "", nodes: [], edges: [] },
    Agent: {
      title: "", description: "",
      model: { provider: "", name: "", temperature: null },
      promptRef: "",
      systemPrompt: "",
      skills: [], tools: [], mcpServers: [], prompts: [],
      limits: { maxTurns: 20, timeoutSeconds: 900 },
      riskLevel: "medium",
      a2a: { url: "", preferredTransport: "" },
    },
  };
  return { apiVersion: API_VERSION, kind, metadata: meta, spec: specByKind[kind] ?? specByKind.Skill };
}

export function editorDocument(
  kind: string,
  published?: Record<string, unknown>,
): Record<string, unknown> {
  return structuredClone(published ?? starter(kind));
}

// ── Manifest linting / safety scan ──────────────────────────────────────────
export interface LintIssue {
  level: "error" | "warning";
  message: string;
}

const NAME_RE = /^[a-z0-9]([-a-z0-9]*[a-z0-9])?$/;
const MAX_NAME = 253;
const MAX_BYTES = 256 * 1024;

const TOP_LEVEL = new Set(["apiVersion", "kind", "metadata", "spec", "status"]);

const UNSAFE: { re: RegExp; what: string }[] = [
  { re: /<script\b/i, what: "embedded <script> markup" },
  { re: /<\/?iframe\b/i, what: "embedded <iframe> markup" },
  { re: /javascript:/i, what: "javascript: URI" },
  { re: /data:text\/html/i, what: "data:text/html URI" },
  { re: /\bon(?:load|error|click|mouseover)\s*=/i, what: "inline event handler" },
];
// eslint-disable-next-line no-control-regex
const CONTROL_RE = /[\u0000-\u0008\u000B\u000C\u000E-\u001F]/;

function requiredPaths(kind: string): Field[] {
  return fieldsFor(kind).filter((f) => f.required);
}

export function lintManifest(doc: unknown, expectedKind: string): LintIssue[] {
  const issues: LintIssue[] = [];
  const err = (message: string) => issues.push({ level: "error", message });
  const warn = (message: string) => issues.push({ level: "warning", message });

  if (doc == null || typeof doc !== "object" || Array.isArray(doc)) {
    return [{ level: "error", message: "manifest must be a single object (apiVersion/kind/metadata/spec)" }];
  }
  const d = doc as Record<string, unknown>;

  let serialized = "";
  try {
    serialized = JSON.stringify(d);
  } catch {
    return [{ level: "error", message: "manifest contains values that cannot be serialized (circular or invalid)" }];
  }
  if (serialized.length > MAX_BYTES) err(`manifest is too large (${Math.round(serialized.length / 1024)}KB; max ${MAX_BYTES / 1024}KB)`);
  if (CONTROL_RE.test(serialized)) err("manifest contains control/binary characters");
  for (const { re, what } of UNSAFE) if (re.test(serialized)) err(`manifest contains unsafe content: ${what}`);

  if (!d.apiVersion) err("missing required field: apiVersion");
  else if (d.apiVersion !== API_VERSION) warn(`apiVersion is "${String(d.apiVersion)}"; expected "${API_VERSION}"`);

  if (!d.kind) err("missing required field: kind");
  else if (typeof d.kind === "string" && d.kind !== expectedKind) err(`kind is "${d.kind}" but this collection only accepts "${expectedKind}"`);

  const meta = d.metadata as Record<string, unknown> | undefined;
  if (!meta || typeof meta !== "object") {
    err("missing required field: metadata");
  } else {
    const name = meta.name;
    if (!name || typeof name !== "string" || !name.trim()) err("missing required field: metadata.name");
    else if (name.length > MAX_NAME) err(`metadata.name is too long (max ${MAX_NAME} chars)`);
    else if (!NAME_RE.test(name)) err(`metadata.name "${name}" is invalid (use lowercase letters, digits and hyphens, e.g. my-skill)`);

    if (meta.visibility != null && !["public", "internal", "private"].includes(String(meta.visibility))) {
      err(`metadata.visibility "${String(meta.visibility)}" is invalid (public | internal | private)`);
    }
    if (meta.labels != null && (typeof meta.labels !== "object" || Array.isArray(meta.labels))) {
      err("metadata.labels must be a string→string map");
    }
  }

  if (d.spec == null || typeof d.spec !== "object" || Array.isArray(d.spec)) {
    err("missing required field: spec");
  } else {
    const spec = d.spec as Record<string, unknown>;
    for (const f of requiredPaths(expectedKind)) {
      if (f.path.startsWith("metadata.")) continue;
      const v = getPath(d, f.path);
      if (v == null || (typeof v === "string" && !v.trim())) err(`missing required field: ${f.path} (${f.label})`);
    }
    if (expectedKind === "Agent") {
      const systemPrompt = spec.systemPrompt;
      const promptRef = spec.promptRef;
      if (systemPrompt != null && typeof systemPrompt !== "string") err("spec.systemPrompt must be a string");
      if (promptRef != null && typeof promptRef !== "string") err("spec.promptRef must be a string");
      const hasInline = typeof systemPrompt === "string" && systemPrompt.trim().length > 0;
      const hasReference = typeof promptRef === "string" && promptRef.trim().length > 0;
      if (!hasInline && !hasReference) {
        err("agent requires an inline system prompt or a prompt reference");
      }

      const model = spec.model;
      if (model == null || typeof model !== "object" || Array.isArray(model)) {
        err("spec.model must be an object");
      } else {
        const typedModel = model as Record<string, unknown>;
        if (typeof typedModel.provider !== "string" || !typedModel.provider.trim()) {
          err("spec.model.provider is required");
        } else if (!AGENT_MODEL_PROVIDERS.has(typedModel.provider)) {
          err(`spec.model.provider must be one of ${[...AGENT_MODEL_PROVIDERS].join(", ")}`);
        }
        if (typeof typedModel.name !== "string" || !typedModel.name.trim()) {
          err("spec.model.name is required");
        }
        if (
          typedModel.temperature != null &&
          (typeof typedModel.temperature !== "number" ||
            !Number.isFinite(typedModel.temperature) ||
            typedModel.temperature < 0 ||
            typedModel.temperature > 2)
        ) {
          err("spec.model.temperature must be a number between 0 and 2");
        }
      }

      const limits = spec.limits;
      if (limits == null || typeof limits !== "object" || Array.isArray(limits)) {
        err("spec.limits must be an object");
      } else {
        const typedLimits = limits as Record<string, unknown>;
        if (!Number.isInteger(typedLimits.maxTurns) || Number(typedLimits.maxTurns) < 1 || Number(typedLimits.maxTurns) > 1000) {
          err("spec.limits.maxTurns must be an integer between 1 and 1000");
        }
        if (
          !Number.isInteger(typedLimits.timeoutSeconds) ||
          Number(typedLimits.timeoutSeconds) < 1 ||
          Number(typedLimits.timeoutSeconds) > 86400
        ) {
          err("spec.limits.timeoutSeconds must be an integer between 1 and 86400");
        }
      }

      if (!AGENT_RISK_LEVELS.has(String(spec.riskLevel ?? ""))) {
        err("spec.riskLevel must be one of low, medium, high, critical");
      }
      const evalSuite = spec.evalSuite;
      if (evalSuite != null) {
        if (typeof evalSuite !== "object" || Array.isArray(evalSuite)) {
          err("spec.evalSuite must be an object");
        } else {
          const ref = evalSuite as Record<string, unknown>;
          if (typeof ref.ref !== "string" || !ref.ref.trim()) err("spec.evalSuite.ref is required");
          if (typeof ref.version !== "string" || !ref.version.trim()) {
            err("spec.evalSuite.version is required");
          }
        }
      }
    }
    if (expectedKind === "Dataset") {
      if (!Array.isArray(spec.cases) || spec.cases.length === 0) {
        err("spec.cases must contain at least one versioned case");
      } else {
        const ids = new Set<string>();
        for (const value of spec.cases) {
          if (value == null || typeof value !== "object" || Array.isArray(value)) {
            err("spec.cases entries must be objects");
            continue;
          }
          const item = value as Record<string, unknown>;
          const id = typeof item.id === "string" ? item.id.trim() : "";
          if (!id) err("spec.cases[].id is required");
          else if (ids.has(id)) err(`spec.cases contains duplicate id "${id}"`);
          else ids.add(id);
          if (typeof item.input !== "string" || !item.input.trim()) err(`spec.cases[${id || "?"}].input is required`);
        }
      }
    }
    if (expectedKind === "EvalSuite") {
      const datasetRef = spec.datasetRef;
      if (datasetRef == null || typeof datasetRef !== "object" || Array.isArray(datasetRef)) {
        err("spec.datasetRef must pin a dataset name and version");
      } else {
        const ref = datasetRef as Record<string, unknown>;
        if (typeof ref.ref !== "string" || !ref.ref.trim()) err("spec.datasetRef.ref is required");
        if (typeof ref.version !== "string" || !ref.version.trim()) err("spec.datasetRef.version is required");
      }
      if (!Array.isArray(spec.scorers) || spec.scorers.length === 0) {
        err("spec.scorers must contain at least one scorer");
      } else {
        const scorerNames = new Set<string>();
        for (const scorer of spec.scorers) {
          if (typeof scorer !== "string" || !scorer.trim()) {
            err("spec.scorers entries must be non-empty strings");
          } else if (scorerNames.has(scorer)) {
            err(`spec.scorers contains duplicate scorer "${scorer}"`);
          } else {
            scorerNames.add(scorer);
          }
        }
      }
      const thresholds = spec.thresholds;
      if (thresholds != null && (typeof thresholds !== "object" || Array.isArray(thresholds))) {
        err("spec.thresholds must be an object");
      } else if (thresholds != null) {
        const values = thresholds as Record<string, unknown>;
        for (const name of ["success", "safety", "hallucination"] as const) {
          const value = values[name];
          if (value != null && (typeof value !== "number" || !Number.isFinite(value) || value < 0 || value > 1)) {
            err(`spec.thresholds.${name} must be a number between 0 and 1`);
          }
        }
        const latency = values.p95_latency_s;
        if (latency != null && (typeof latency !== "number" || !Number.isFinite(latency) || latency <= 0)) {
          err("spec.thresholds.p95_latency_s must be a positive number");
        }
        const cost = values.cost_per_run_usd;
        if (cost != null && (typeof cost !== "number" || !Number.isFinite(cost) || cost < 0)) {
          err("spec.thresholds.cost_per_run_usd must be a non-negative number");
        }
      }
    }
  }

  for (const k of Object.keys(d)) if (!TOP_LEVEL.has(k)) warn(`unexpected top-level field "${k}" will be ignored`);

  return issues;
}

// getPath / setPath operate on dot-paths, creating intermediate objects/arrays.
export function getPath(obj: unknown, path: string): unknown {
  return path.split(".").reduce<unknown>((acc, key) => {
    if (acc == null) return undefined;
    return (acc as Record<string, unknown>)[key];
  }, obj);
}

export function setPath(obj: Record<string, unknown>, path: string, value: unknown): Record<string, unknown> {
  const keys = path.split(".");
  const clone = structuredClone(obj);
  let cur: Record<string, unknown> = clone;
  for (let i = 0; i < keys.length - 1; i++) {
    const k = keys[i];
    const nextIsIndex = /^\d+$/.test(keys[i + 1]);
    if (cur[k] == null) cur[k] = nextIsIndex ? [] : {};
    cur = cur[k] as Record<string, unknown>;
  }
  cur[keys[keys.length - 1]] = value;
  return clone;
}

/** plural collection name for a kind (matches the registry HTTP routes). */
export function pluralForKind(kind: string): string {
  switch (kind) {
    case "MCPServer":
      return "mcp-servers";
    case "Skill":
      return "skills";
    case "Tool":
      return "tools";
    case "Prompt":
      return "prompts";
    case "Workflow":
      return "workflows";
    case "Blueprint":
      return "blueprints";
    case "Agent":
      return "agents";
    case "Dataset":
      return "datasets";
    case "EvalSuite":
      return "eval-suites";
    default:
      return `${kind.toLowerCase()}s`;
  }
}
