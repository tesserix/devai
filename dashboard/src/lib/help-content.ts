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
      "1. Connect a repository under Repositories — DevAI can only work on repos you have onboarded. 2. Open Workflows and pick one, or use Compose to describe the job in plain English. 3. Come back here to watch it run step by step.",
      "This page shows one run at a time. Pick a run on the left and the stages, agents and messages for it fill in on the right, live.",
    ],
    links: [
      { label: "Connect a repository", href: "/repos" },
      { label: "Pick a workflow", href: "/workflows" },
      { label: "Describe a task instead", href: "/compose" },
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
