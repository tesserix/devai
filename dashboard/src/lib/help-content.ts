import { NEW_RUN_HREF } from "./run-entry.ts";

/**
 * help-content.ts — the single source of truth for in-context guidance.
 *
 * Every explanation here is written to match how DevAI ACTUALLY behaves, not
 * how a marketing page wishes it did. The two most-misunderstood concepts are
 * (1) "Workflows" vs "Blueprints" vs "Board" and (2) what "dynamic" means for
 * the supervisor — both are spelled out honestly below.
 *
 * Consumers:
 *   - <GuidancePanel id="…">  reads the matching GuidanceEntry (a dismissible
 *     explainer card at the top of a page).
 *   - <HelpPopover term="…">  reads the matching HelpTerm (the "?" tooltip).
 *
 * Keep the copy crisp, concrete, and free of overpromising. If a feature is
 * advisory or best-effort, say so.
 */

export interface HelpLink {
  label: string;
  href: string;
}

/** A page-level explainer surfaced by <GuidancePanel>. */
export interface GuidanceEntry {
  /** Stable key — also the localStorage dismiss key (`devai-guidance:<id>`). */
  id: string;
  /** Short heading shown in the card. */
  title: string;
  /** 1–3 short paragraphs. Plain strings (no markup) — rendered as <p>. */
  body: string[];
  /** Optional cross-links to related pages. */
  links?: HelpLink[];
}

/** An inline term explained by <HelpPopover term="…">. */
export interface HelpTerm {
  /** Stable key. */
  term: string;
  /** Human label shown as the popover heading. */
  label: string;
  /** One tight paragraph. */
  summary: string;
  /** Optional bullet list of specifics. */
  points?: string[];
}

// ─────────────────────────────────────────────────────────────────────────
// Page-level guidance (GuidancePanel)
// ─────────────────────────────────────────────────────────────────────────

export const GUIDANCE: Record<string, GuidanceEntry> = {
  home: {
    id: "home",
    title: "New here? Three steps to your first run",
    body: [
      "1. Connect a repository under Repositories — DevAI can only work on repos you have onboarded. 2. Open Workflows and pick one, or describe the job in plain English from the new-run dialog. 3. Open the live run to watch it work step by step.",
      "This page shows one run at a time. Pick a run on the left and the stages, agents and messages for it fill in on the right, live.",
    ],
    links: [
      { label: "Connect a repository", href: "/repos" },
      { label: "Pick a workflow", href: "/workflows" },
      { label: "Describe a task instead", href: NEW_RUN_HREF },
    ],
  },

  compose: {
    id: "compose",
    title: "Compose: describe the job in plain English",
    body: [
      "Type what you want done, pick the repository and the workflow to run it with, and DevAI assigns a crew and starts. Use @ to point at specific files or teammates, and attach files for extra context.",
      "The terminal streams what the crew is doing as it happens, and the timeline below records the git checkpoints you can roll back to.",
    ],
    links: [
      { label: "Prefer picking a ready-made workflow?", href: "/workflows" },
      { label: "See past runs", href: "/runs" },
    ],
  },

  repos: {
    id: "repos",
    title: "Repositories: start here",
    body: [
      "DevAI only works on repositories you have onboarded. Onboarding opens a pull request that adds a small .platform/devai.yaml marker, and caches the repo's tech stack so runs start faster.",
      "Select one or more repos and choose Onboard. Once the PR is merged the repo shows as Onboarded and becomes selectable everywhere else in the app.",
    ],
    links: [{ label: "Then run a workflow", href: "/workflows" }],
  },

  agents: {
    id: "agents",
    title: "Agents: the workers you can assign",
    body: [
      "An agent is a role — senior developer, QA tester, security expert — with its own skills, tools and language model. Workflow stages hand work to agents.",
      "The ones shipped by default cover the whole lifecycle, so you usually do not need to create any. Build your own only when you need a role that does not exist yet.",
    ],
    links: [
      { label: "Skills an agent can use", href: "/skills" },
      { label: "Tools an agent can call", href: "/tools" },
    ],
  },

  "agent-workbench": {
    id: "agent-workbench",
    title: "Develop an agent without touching the cluster",
    body: [
      "A sandbox pins one agent version, model, tool policy, budget and expiry. Playground turns and evaluation cases run through that same isolated configuration, so a comparison is about the agent change rather than a moving dependency.",
      "Start in Playground, inspect the exact model and tool steps under Traces, run a versioned dataset under Evaluations, then compare the durable run with your production baseline. Destroying the sandbox removes its runtime but keeps the evidence.",
    ],
    links: [
      { label: "Manage evaluation artifacts", href: "/registry" },
      { label: "See all sandboxes", href: "/sandboxes" },
    ],
  },

  skills: {
    id: "skills",
    title: "Skills: reusable know-how",
    body: [
      "A skill is a piece of capability you can attach to more than one agent, so you describe it once instead of repeating it in every agent.",
      "Skills do nothing on their own. Attach them to an agent, then use that agent in a workflow stage.",
    ],
    links: [{ label: "Attach one to an agent", href: "/agents" }],
  },

  tools: {
    id: "tools",
    title: "Tools: what agents can actually call",
    body: [
      "A tool is an external capability an agent can invoke — a custom MCP server exposing things like a database query or an internal API. Registering one makes it reachable to agents at /mcp/<name>.",
      "Like skills, tools do nothing by themselves. Give one to an agent, then use that agent in a workflow.",
    ],
    links: [{ label: "Give a tool to an agent", href: "/agents" }],
  },

  prompts: {
    id: "prompts",
    title: "Prompts: shared wording",
    body: [
      "Prompts are reusable instruction templates that agents pull in, so the same wording is not copy-pasted into several agents and left to drift.",
    ],
    links: [{ label: "Used by agents", href: "/agents" }],
  },

  registry: {
    id: "registry",
    title: "Browse everything in one place",
    body: [
      "The catalog lists every agent, skill, tool, prompt and blueprint available to this workspace. It is the search-everything view — the dedicated pages in the sidebar are the same content, filtered by kind.",
    ],
  },

  gateway: {
    id: "gateway",
    title: "Service health: is the platform itself up?",
    body: [
      "This page checks the plumbing DevAI depends on — the registry, the agent gateway, and the language-model proxy — and reports whether each one is reachable right now.",
      "Come here when runs fail for no obvious reason. If something shows as unreachable the problem is the platform, not your workflow.",
    ],
    links: [{ label: "Check recent runs", href: "/runs" }],
  },

  analytics: {
    id: "analytics",
    title: "Analytics: how the platform is performing",
    body: [
      "Run and stage success rates, how busy each agent is, and what the language models are costing you in tokens and money.",
      "Each section loads on its own. If one is empty its data source has nothing yet or is unreachable — the rest of the page is still accurate.",
    ],
  },

  logs: {
    id: "logs",
    title: "Logs: live output, reliability, and history",
    body: [
      "Live logs is a rolling window of what the API is doing right now — a 'now' view, not searchable history. SLOs tracks availability, latency and error rates against their targets. Archive is the long-term copy kept in cloud storage.",
      "Debugging a run that just failed? Start with Live logs. Looking for something from last week? Use Archive.",
    ],
  },

  "sre-studio": {
    id: "sre-studio",
    title: "SRE Studio: monitoring rules",
    body: [
      "Author and publish the configuration the SRE agents use to watch your clusters. You can dry-run a change here to see what it would do before publishing it.",
      "This is separate from the ALM workflows in the rest of the app — it governs production monitoring, not code delivery.",
    ],
  },

  settings: {
    id: "settings",
    title: "Settings",
    body: [
      "Workspace-wide configuration. Changes here affect every future run, not the ones already in flight.",
    ],
  },

  workflows: {
    id: "workflows",
    title: "Workflows: define → run → observe",
    body: [
      "A workflow is a blueprint — a YAML DAG of stages that runs against a repository. Pick a blueprint, give it a repo and an intent, and DevAI dispatches a run that walks the stages in dependency order.",
      "Each stage runs an agent or a crew, can depend on earlier stages, may be skipped by a condition, and can pause on a human-approval gate. The DAG itself is fixed for a given blueprint — runs differ by which stages are skipped, not by adding or reordering stages.",
    ],
    links: [
      { label: "Browse all blueprints", href: "/blueprint" },
      { label: "Recent runs", href: "/runs" },
      { label: "Build a blueprint", href: "/workflows/new" },
    ],
  },

  board: {
    id: "board",
    title: "Board: the GitHub issue Kanban",
    body: [
      "The Board mirrors GitHub issues across your onboarded repos as a Kanban. It is for triage and tracking — it does not execute anything.",
      "To turn work into an automated run, open Workflows, choose a blueprint, and dispatch it against the repo. A card here can link to the run that is working it.",
    ],
    links: [
      { label: "Run a workflow", href: "/workflows" },
      { label: "Recent runs", href: "/runs" },
    ],
  },

  blueprints: {
    id: "blueprints",
    title: "Blueprints: the catalog of runnable DAGs",
    body: [
      "Blueprints are the workflow definitions DevAI can run — alm-pipeline, supervisor-alm, crew-task, app-scaffold, pr-review, security-scan and the SRE blueprints. Each one is a stage DAG with its own gates, conditions and timeouts.",
      "This page is read-and-author. To dispatch one against a repo, use Run on the blueprint or the New task dialog. To create your own, use the builder.",
    ],
    links: [
      { label: "Run a workflow", href: "/workflows" },
      { label: "Build a blueprint", href: "/workflows/new" },
    ],
  },

  runs: {
    id: "runs",
    title: "Runs: every execution of a blueprint",
    body: [
      "A run is one execution of a blueprint against a repo. It carries the stages completed, skipped and failed, a timeline of stage events, the handover bag passed between stages, and git checkpoints.",
      "Open a run to watch its DAG fill in live, approve or reject gates, inspect the agent handover, and (where the blueprint provides one) open a live preview.",
    ],
    links: [{ label: "Run a new workflow", href: "/workflows" }],
  },

  // Keyed by the id the page passes to <GuidancePanel>, not camelCase — the
  // lookup is a plain record access, so a mismatched key silently renders nothing.
  "run-detail": {
    id: "run-detail",
    title: "Reading a run",
    body: [
      "The DAG overlays live state onto the blueprint: stages turn green when done, ring while running, redden on failure, and grey out when a condition skipped them. Gate stages pause until someone approves or rejects.",
      "Progress streams over SSE, so the view updates without a refresh. The handover bag shows what each stage passed downstream; checkpoints are the git SHAs you can roll back to.",
    ],
  },

  builder: {
    id: "builder",
    title: "Building a blueprint",
    body: [
      "Lay out stages as a DAG: each stage runs an agent (a specialization) or a crew, with depends_on edges, an optional condition, a timeout, and an on-failure policy (stop, rollback or continue).",
      "Pick a crew or a valid agent name for every stage — an empty crew or an unknown agent silently no-ops at runtime. Conditions are evaluated against the handover bag; they can only skip a stage, never add one.",
    ],
    links: [{ label: "See existing blueprints", href: "/blueprint" }],
  },

  preview: {
    id: "preview",
    title: "Live previews",
    body: [
      "A preview is an on-demand, ephemeral environment at preview-<id>.tesserix.app. DevAI provisions a volume, clones the repo, installs dependencies and boots a dev server (plus a backend and seeded Postgres when the repo needs them).",
      "Bring-up runs clone → install → boot and can take a few minutes — the building state shows per-container health while it comes up. Idle previews are reaped after a TTL (about 4 hours), so stop one when you are done.",
    ],
  },
};

// ─────────────────────────────────────────────────────────────────────────
// Inline term help (HelpPopover)
// ─────────────────────────────────────────────────────────────────────────

export const HELP_TERMS: Record<string, HelpTerm> = {
  coordination: {
    term: "coordination",
    label: "Coordination Layer",
    summary:
      "The two coordinator agents that run the show: the Supervisor plans the work and delegates to specialists; the Orchestrator sequences execution (code → test → fix → deploy) and tracks phase progress.",
    points: [
      "They don't write code — they route, track, and decide.",
      "'N msgs' counts the agent-to-agent messages each sent/received this run.",
      "In a boardroom debate the Supervisor moderates and takes the notes.",
    ],
  },

  specialists: {
    term: "specialists",
    label: "Specialist Agents",
    summary:
      "The role agents that do the actual work, each with its own skills, tools, and LLM. A card lights up while its stage runs; 'N msgs' is its agent-to-agent traffic; Done/Idle/Failed reflects its latest stage outcome.",
    points: [
      "Analysis: document analyzer, tech detector, requirements analyst.",
      "Planning: product director (epic + stories), engineering manager.",
      "Build/Quality/Deploy: senior developer, DB engineer, reviewers, QA, CI monitor, infra, release.",
    ],
  },


  workflow: {
    term: "workflow",
    label: "Workflow / Blueprint",
    summary:
      "A workflow is a blueprint: a YAML DAG of stages that DevAI runs against a repo. The two words mean the same thing here — Blueprints is the catalog, Workflows is where you run them.",
    points: [
      "Stages run in dependency order (depends_on).",
      "A stage runs an agent, a crew, or a specialization.",
      "The DAG is fixed per blueprint; runs differ by which stages skip.",
    ],
  },

  blueprint: {
    term: "blueprint",
    label: "Blueprint",
    summary:
      "A blueprint is the YAML definition of a workflow — its stages, their dependencies, gates, conditions and timeouts. Running a blueprint creates a run.",
    points: [
      "~10 ship by default (alm-pipeline, supervisor-alm, crew-task, app-scaffold, pr-review, security-scan, sre-*).",
      "You can author your own in the builder.",
    ],
  },

  run: {
    term: "run",
    label: "Run",
    summary:
      "A run is one execution of a blueprint against a repo. It tracks completed / skipped / failed stages, a stage-event timeline, the agent handover bag, and git checkpoints.",
    points: [
      "Dispatched → queued → executed by an async worker.",
      "Progress streams live over SSE.",
    ],
  },

  stage: {
    term: "stage",
    label: "Stage",
    summary:
      "A stage is one node in the DAG. It runs an agent or crew, may depend on earlier stages, can be skipped by a condition, has a timeout, and an on-failure policy.",
    points: [
      "Execution per stage: skip-if-done → condition gate → run with timeout → emit event → merge handover → advance → apply on_failure.",
      "on_failure is one of stop, rollback or continue.",
    ],
  },

  gate: {
    term: "gate",
    label: "Approval gate",
    summary:
      "A gate stage blocks the run on a durable human approve/reject decision. Nothing downstream runs until someone decides — the choice survives restarts.",
    points: [
      "Approve to continue; reject to stop the run.",
      "Used for sensitive stages like security and deploy.",
    ],
  },

  condition: {
    term: "condition",
    label: "Condition",
    summary:
      "A condition is a small expression evaluated against the handover bag before a stage runs. If it is false, the stage is skipped. Conditions can only skip stages — never add, remove or reorder them.",
  },

  handover: {
    term: "handover",
    label: "Handover bag (agent_context)",
    summary:
      "The handover bag is the shared context that flows through a run. Each stage's output is merged in, so downstream stages can read what came before.",
  },

  // Keyed exactly as the page passes it — getHelpTerm is a plain record lookup,
  // so a missing or mis-keyed entry renders nothing at all.
  "delegation-plan": {
    term: "delegation-plan",
    label: "Supervisor delegation plan",
    summary:
      "How the supervisor would split this work across agents, written before the run. It is advisory: the stages that actually execute are the blueprint's fixed DAG, and nothing here adds, removes or reorders them.",
    points: [
      "Read it to understand the supervisor's reasoning, not to predict the stage list.",
      "Assignments name the agent role the supervisor thinks each piece belongs to.",
      "A tracking issue link appears when the supervisor opened one on the repo.",
    ],
  },

  dynamic: {
    term: "dynamic",
    label: 'What "dynamic" really means',
    summary:
      "The DAG topology is fixed at runtime — \"dynamic\" never rewrites it. There are two honest senses of the word, and neither adds or reorders stages.",
    points: [
      "Conditional skipping: a stage's condition can skip it.",
      "Intra-stage planning: the run-crew lead LLM assigns members inside the single crew node.",
      "Supervisor: emits an advisory delegation_plan (plus a GitHub tracking issue). The plan is reasoning you can read — it does NOT change which stages run.",
    ],
  },

  delegationPlan: {
    term: "delegation-plan",
    label: "Delegation plan (advisory)",
    summary:
      "The supervisor stage produces a delegation_plan describing how it would split the work. It is advisory only: the executed stages are still the fixed supervisor-alm DAG. We surface it so the reasoning is visible.",
  },

  crew: {
    term: "crew",
    label: "Crew",
    summary:
      "A crew is a lead agent plus members that run inside a single run-crew stage. The lead LLM-assigns members at runtime within that one node — it does not spawn new DAG stages.",
  },

  preview: {
    term: "preview",
    label: "Live preview",
    summary:
      "An ephemeral environment at preview-<id>.tesserix.app, iframed in the run's Preview tab. Bring-up is clone → install → boot and can take minutes; idle previews are reaped after ~4h.",
    points: [
      "Started on demand, or by the app-scaffold blueprint's preview stage.",
      "Routed through the session-gated auth-bff.",
    ],
  },

  checkpoint: {
    term: "checkpoint",
    label: "Checkpoint",
    summary:
      "A checkpoint is a git SHA captured during a run. It marks a point you can roll the working tree back to.",
  },

  sandbox: {
    term: "sandbox",
    label: "Agent sandbox",
    summary:
      "An ephemeral, owner-scoped runtime with a pinned agent, model, prompt, dataset, tool policy, token/cost budget and TTL. It uses your explicitly selected user connector through AgentGateway; credentials are not mounted into the sandbox.",
    points: [
      "Mock or block side-effecting tools unless real access is explicitly selected.",
      "The runtime expires automatically and can be destroyed at any time.",
      "Traces and evaluation results are durable and remain after destruction.",
    ],
  },

  evaluation: {
    term: "evaluation",
    label: "Agent evaluation",
    summary:
      "A versioned dataset of cases run against one pinned sandbox, scored for expected output, tool trajectory, safety, groundedness, latency, tokens and cost. A failing score links to the trace that explains it.",
  },

  "evaluation-pass-rate": {
    term: "evaluation-pass-rate",
    label: "Pass rate",
    summary:
      "The share of dataset cases that met every configured scorer and threshold; one failed required dimension makes that case fail.",
    points: ["Read the failing case trace before changing the agent—the percentage only tells you where to look."],
  },

  "evaluation-deterministic-score": {
    term: "evaluation-deterministic-score",
    label: "Deterministic score",
    summary:
      "A direct check of an observable result, such as exact or regex output, valid JSON, task completion, or an expected tool call.",
    points: ["These checks are fast and repeatable because no judge model is involved."],
  },

  "evaluation-scorer-dimension": {
    term: "evaluation-scorer-dimension",
    label: "Scorer dimension",
    summary:
      "The average 0–100% score for one configured quality dimension across all cases; its threshold, not the average alone, determines which cases pass.",
    points: ["A high average can hide one important failure, so open the case-level trace."],
  },

  "evaluation-groundedness": {
    term: "evaluation-groundedness",
    label: "Groundedness",
    summary:
      "A judge-model score for how well factual claims are supported by the evidence supplied to the agent, rather than invented or assumed.",
    points: ["Treat it as model-graded evidence, then inspect the trace and cited context for a failing case."],
  },

  "evaluation-safety-score": {
    term: "evaluation-safety-score",
    label: "Safety score",
    summary:
      "The share of cases without a forbidden, blocked, or policy-violating tool attempt; a blocked attempt still counts because intent matters.",
    points: ["Safety is normally a hard gate: one unsafe case should block promotion."],
  },

  "evaluation-trajectory-score": {
    term: "evaluation-trajectory-score",
    label: "Tool trajectory",
    summary:
      "Whether the agent chose the expected tools, arguments, and order without redundant calls, and recovered safely when a tool failed.",
    points: ["Trajectory can fail even when the final prose looks correct because the path produced unsafe or incorrect side effects."],
  },

  "evaluation-p95-latency": {
    term: "evaluation-p95-latency",
    label: "P95 latency",
    summary:
      "The case latency at the 95th percentile: about 95% of cases completed this quickly or faster, while the slowest tail may take longer.",
    points: ["Small datasets make percentiles coarse; compare repeated runs before treating a small delta as real."],
  },

  "evaluation-tokens": {
    term: "evaluation-tokens",
    label: "Total tokens",
    summary:
      "All model input and output tokens consumed by the evaluation cases, used to spot prompt growth, loops, and likely cost changes.",
    points: ["Judge-model tokens are reflected in attributed cost and may be reported separately from the agent's own usage."],
  },

  "evaluation-cost": {
    term: "evaluation-cost",
    label: "Attributed evaluation cost",
    summary:
      "The recorded evaluation spend split into agent model calls, judge model calls, and sandbox infrastructure so costs are not hidden or double-counted.",
    points: ["This is owner-scoped usage for the pinned connector and sandbox configuration."],
  },

  "evaluation-delta": {
    term: "evaluation-delta",
    label: "Candidate delta",
    summary:
      "Candidate minus baseline for the same immutable dataset; higher is better for quality scores, while lower is better for latency, tokens, and cost.",
  },

  "evaluation-regression": {
    term: "evaluation-regression",
    label: "Regression",
    summary:
      "A case that passed in the baseline run but fails in the candidate run; open its candidate trace to find the first meaningful divergence.",
  },

  "evaluation-sample-size": {
    term: "evaluation-sample-size",
    label: "Sample-size caveat",
    summary:
      "One paired run shows directional change, not statistical certainty; small suites and non-deterministic models need repeated runs before promotion.",
  },

  "evaluation-comparison": {
    term: "evaluation-comparison",
    label: "Evaluation comparison",
    summary:
      "A baseline and candidate evaluation run over the same immutable dataset. DevAI shows metric deltas, newly passing cases and exact regressions; it refuses comparisons whose dataset versions differ.",
  },

  onboarding: {
    term: "onboarding",
    label: "Repo onboarding",
    summary:
      "Onboarding drops a .platform/devai.yaml marker into a repo (via a PR) so DevAI recognises it and caches its tech-stack profile before any run starts.",
  },
};

/** Lookup helpers — return undefined for unknown ids so callers can no-op. */
export function getGuidance(id: string): GuidanceEntry | undefined {
  return GUIDANCE[id];
}

export function getHelpTerm(term: string): HelpTerm | undefined {
  return HELP_TERMS[term];
}

const EVALUATION_METRIC_TERMS: Record<string, string> = {
  success: "evaluation-pass-rate",
  pass_rate: "evaluation-pass-rate",
  p95_latency_ms: "evaluation-p95-latency",
  latency: "evaluation-p95-latency",
  total_tokens: "evaluation-tokens",
  tokens: "evaluation-tokens",
  cost_usd: "evaluation-cost",
  cost: "evaluation-cost",
  exact_match: "evaluation-deterministic-score",
  regex: "evaluation-deterministic-score",
  json_schema: "evaluation-deterministic-score",
  expected_tool_call: "evaluation-deterministic-score",
  task_completion: "evaluation-deterministic-score",
  tool_trajectory: "evaluation-trajectory-score",
  safety: "evaluation-safety-score",
  groundedness: "evaluation-groundedness",
  hallucination: "evaluation-groundedness",
};

/** Resolve a runtime scorer/summary key to help that remains useful for custom dimensions. */
export function evaluationMetricHelpTerm(metric: string): string {
  return EVALUATION_METRIC_TERMS[metric.toLowerCase()] ?? "evaluation-scorer-dimension";
}
