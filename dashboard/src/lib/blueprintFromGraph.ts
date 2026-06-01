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
//       config: { specialization: <agent name> }   # for run_specialization

export interface BuilderStage {
  id: string; // internal canvas id (not serialized)
  name: string; // stage name — the depends_on key
  type: string;
  stageKey: string; // the factory key
  agent?: string; // run_specialization → config.specialization
  dependsOn: string[]; // upstream stage names
}

// Stage types the loader recognizes (informational; loader is permissive).
export const STAGE_TYPES = ["agentic", "deterministic", "review", "analyst", "context", "deploy"];

// A curated set of registered stage factory keys. `run_specialization` runs a
// composed agent (config.specialization); the rest are lifecycle stages.
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

const NAME_RE = /^[a-z][a-z0-9-]{1,63}$/;

export interface ConvertResult {
  yaml: string;
  errors: string[];
}

/** Validate + serialize the builder graph into blueprint YAML. */
export function blueprintFromGraph(
  name: string,
  description: string,
  stages: BuilderStage[],
): ConvertResult {
  const errors: string[] = [];
  if (!NAME_RE.test(name)) errors.push("Blueprint name must be lowercase kebab-case (e.g. my-flow).");
  if (stages.length === 0) errors.push("Add at least one stage.");

  const names = new Set<string>();
  for (const s of stages) {
    if (!NAME_RE.test(s.name)) errors.push(`Stage name "${s.name}" must be lowercase kebab-case.`);
    if (names.has(s.name)) errors.push(`Duplicate stage name "${s.name}".`);
    names.add(s.name);
    if (!s.stageKey) errors.push(`Stage "${s.name}" needs a stage type (factory key).`);
    if (s.stageKey === "run_specialization" && !s.agent) {
      errors.push(`Stage "${s.name}" runs a specialization — pick an agent.`);
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
    if (s.stageKey === "run_specialization" && s.agent) {
      lines.push(`    config:`);
      lines.push(`      specialization: ${s.agent}`);
    }
  }
  return { yaml: lines.join("\n") + "\n", errors: [] };
}

// Quote a scalar only when it contains YAML-significant characters.
function yamlScalar(v: string): string {
  if (/^[\w .,/()'-]+$/.test(v)) return v;
  return JSON.stringify(v); // valid YAML double-quoted form
}
