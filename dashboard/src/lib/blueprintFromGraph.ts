// Converts the visual blueprint builder's stage graph into a devai blueprint
// YAML document (the shape devai/blueprint/loader.py parses). Kept pure so the
// builder page stays declarative and this is unit-testable.
//
// devai blueprint YAML:
//   name: <id>
//   description: <text>
//   stages:
//     - name: <stage id, used in depends_on>
//       type: deterministic | agentic | review | analyst | context | deploy
//       stage: <registered factory key, e.g. run_specialization>
//       depends_on: [<other stage names>]
//       condition: <tiny expr, e.g. task.has_pr>      # optional skip guard
//       config:
//         specialization: <agent name>                # for run_specialization
//         crew: <crew name>                           # for run_crew

export interface BuilderStage {
  id: string; // internal canvas id (not serialized)
  name: string; // stage name — the depends_on key
  type: string;
  stageKey: string; // the factory key
  agent?: string; // run_specialization → config.specialization
  crew?: string; // run_crew → config.crew
  condition?: string; // optional condition: skip-guard expression
  dependsOn: string[]; // upstream stage names
}

// Stage types the loader recognizes (informational; loader is permissive).
export const STAGE_TYPES = ["agentic", "deterministic", "review", "analyst", "context", "deploy"];

// A curated set of registered stage factory keys. `run_specialization` runs a
// composed agent (config.specialization); `run_crew` runs a lead+members crew
// (config.crew); the rest are lifecycle stages.
export const STAGE_KEYS = [
  "run_specialization",
  "run_crew",
  "context_hydration",
  "memory_injection",
  "review_code",
  "security_scan",
  "run_tests",
  "post_report",
  "noop",
];

// Seed crews bundled with the runtime (crews/*.yaml). The picker also merges in
// any DB-backed crews discovered from the user's teams, but these always exist
// so a None crew never silently no-ops.
export const SEED_CREWS = ["backend_crew", "frontend_crew", "sre_crew"];

// ── Condition DSL (mirrors src/devai/blueprint/conditions.py) ───────────────
// Bare keys are validated at load time against this exact set; a typo would
// otherwise make a gate run unconditionally. Prefixed keys (output. / state. /
// agent_context.) resolve dynamically at runtime and are intentionally not
// validated here — they stay fail-open for forward-compat.
export const TASK_BOOL_KEYS = [
  "task.has_pr",
  "task.has_issue",
  "task.has_sandbox",
  "task.has_epic",
  "task.has_stories",
  "task.is_terminal",
  "task.is_failed",
];
const TASK_BOOL_SET = new Set(TASK_BOOL_KEYS);
const FAIL_OPEN_PREFIXES = ["output.", "state.", "agent_context."];

const NAME_RE = /^[a-z][a-z0-9-]{1,63}$/;

export interface ConvertResult {
  yaml: string;
  errors: string[];
}

/**
 * Validate a single condition expression the same way the backend loader does
 * (devai/blueprint/conditions.py:validate_condition). Returns the list of
 * unknown *bare* keys; an empty list means the condition is structurally usable.
 *
 * Grammar is tiny: atoms joined by `and`/`or`, an optional leading `!`. We only
 * surface unknown bare keys — that is exactly what would cause a gate to run
 * unconditionally at runtime, so blocking publish on it is honest.
 */
export function validateConditionKeys(condition: string | undefined | null): string[] {
  const expr = (condition ?? "").trim();
  if (!expr) return [];
  const unknown: string[] = [];
  for (const tok of expr.split(/\s+/)) {
    const low = tok.toLowerCase();
    if (low === "and" || low === "or" || tok === "!") continue;
    const key = tok.startsWith("!") ? tok.slice(1) : tok;
    if (!key) continue;
    if (TASK_BOOL_SET.has(key)) continue;
    if (FAIL_OPEN_PREFIXES.some((p) => key.startsWith(p))) continue;
    unknown.push(key);
  }
  return unknown;
}

/**
 * Validate + serialize the builder graph into blueprint YAML.
 *
 * `knownAgents` — the set of registered specialization names (api.listRegistryAgents).
 *   When provided, run_specialization stages referencing an unknown agent are
 *   blocked at publish (a typo'd agent silently no-ops at runtime otherwise).
 *   Omit (or pass undefined) to skip that check — e.g. before the list loads.
 * `knownCrews` — the set of resolvable crew names (seed crews + DB crews). Same
 *   reasoning: an unknown/empty crew makes run_crew fail with `no_crew`.
 */
export function blueprintFromGraph(
  name: string,
  description: string,
  stages: BuilderStage[],
  opts?: { knownAgents?: Set<string>; knownCrews?: Set<string> },
): ConvertResult {
  const errors: string[] = [];
  if (!NAME_RE.test(name)) errors.push("Blueprint name must be lowercase kebab-case (e.g. my-flow).");
  if (stages.length === 0) errors.push("Add at least one stage.");

  const knownAgents = opts?.knownAgents;
  const knownCrews = opts?.knownCrews;

  const names = new Set<string>();
  for (const s of stages) {
    if (!NAME_RE.test(s.name)) errors.push(`Stage name "${s.name}" must be lowercase kebab-case.`);
    if (names.has(s.name)) errors.push(`Duplicate stage name "${s.name}".`);
    names.add(s.name);
    if (!s.stageKey) errors.push(`Stage "${s.name}" needs a stage type (factory key).`);

    if (s.stageKey === "run_specialization") {
      const agent = (s.agent ?? "").trim();
      if (!agent) {
        errors.push(`Stage "${s.name}" runs a specialization — pick an agent.`);
      } else if (knownAgents && knownAgents.size > 0 && !knownAgents.has(agent)) {
        errors.push(
          `Stage "${s.name}" references unknown agent "${agent}". Pick a registered agent — an unknown one silently no-ops at runtime.`,
        );
      }
    }

    if (s.stageKey === "run_crew") {
      const crew = (s.crew ?? "").trim();
      if (!crew) {
        errors.push(
          `Stage "${s.name}" runs a crew — pick a crew. An empty crew resolves to no_crew and the stage does nothing.`,
        );
      } else if (knownCrews && knownCrews.size > 0 && !knownCrews.has(crew)) {
        errors.push(
          `Stage "${s.name}" references unknown crew "${crew}". Pick a seed or team crew that exists.`,
        );
      }
    }

    const unknownKeys = validateConditionKeys(s.condition);
    if (unknownKeys.length) {
      errors.push(
        `Stage "${s.name}" condition references unknown key${unknownKeys.length > 1 ? "s" : ""} ${unknownKeys
          .map((k) => `"${k}"`)
          .join(", ")}. Use a task.* flag (e.g. task.has_pr) or an output./state./agent_context. lookup.`,
      );
    }
  }
  for (const s of stages) {
    for (const dep of s.dependsOn) {
      if (!names.has(dep)) errors.push(`Stage "${s.name}" depends on unknown stage "${dep}".`);
    }
  }
  if (errors.length) return { yaml: "", errors };

  const lines: string[] = [];
  lines.push(`name: ${name}`);
  if (description.trim()) lines.push(`description: ${yamlScalar(description.trim())}`);
  lines.push("stages:");
  for (const s of stages) {
    lines.push(`  - name: ${s.name}`);
    lines.push(`    type: ${s.type || "deterministic"}`);
    lines.push(`    stage: ${s.stageKey}`);
    if (s.dependsOn.length) {
      lines.push(`    depends_on: [${s.dependsOn.join(", ")}]`);
    }
    const condition = (s.condition ?? "").trim();
    if (condition) {
      lines.push(`    condition: ${yamlScalar(condition)}`);
    }
    if (s.stageKey === "run_specialization" && s.agent?.trim()) {
      lines.push(`    config:`);
      lines.push(`      specialization: ${s.agent.trim()}`);
    } else if (s.stageKey === "run_crew" && s.crew?.trim()) {
      lines.push(`    config:`);
      lines.push(`      crew: ${s.crew.trim()}`);
    }
  }
  return { yaml: lines.join("\n") + "\n", errors: [] };
}

// Quote a scalar only when it contains YAML-significant characters.
function yamlScalar(v: string): string {
  if (/^[\w .,/()'-]+$/.test(v)) return v;
  return JSON.stringify(v); // valid YAML double-quoted form
}
