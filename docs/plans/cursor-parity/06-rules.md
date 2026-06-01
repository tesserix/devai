# 06 — Rules

**Cursor parity:** Rules (`.cursor/rules`). **Priority: P1.**

## What Cursor does

Reusable, **scoped** system instructions that steer the agent: coding
conventions, preferred libraries, workflow patterns. Persist across sessions;
attach globally, by glob/path, or on‑demand.

## How it works (concepts to steal)

1. **Rules as versioned repo files** (`.cursor/rules/*.mdc`) — reviewed like code.
2. **Scoping/attachment.** Always‑on, auto‑attached by file glob, or
   agent‑requested. Keeps the prompt lean — only relevant rules load.
3. **Layering.** Org rules + repo rules + task rules compose.

## DevAI mapping (framework)

DevAI already has **`specializations/`** (26 YAML role personas) — that's the
"who the agent is". Rules are the "how it must behave **here**" layer, scoped to a
repo. Add a thin rules layer that composes with specializations.

- **`rules/` loader** reads `.devai/rules/*.yaml|md` from the target repo (via
  `scm/`) + a global org rules dir.
- **Scoping engine:** `always | globs: [...] | manual`. For a given task/files,
  select the matching rules.
- **Composition:** final system prompt = org rules ⊕ repo rules ⊕ specialization
  persona ⊕ recalled memories (plan 05). One assembly point in `StageDeps`.
- **Budget guard:** cap total rule tokens; warn when exceeded (Cursor's lesson:
  too many always‑on rules/tools blow the context budget).

## Implementation plan

- **Phase 1 — schema + loader** (`rules/loader.py`, `rules/model.py`).
- **Phase 2 — scoping** (glob match against changed files / task).
- **Phase 3 — prompt assembly** integrating specialization + memory + rules with a
  token budget.
- **Phase 4 — authoring UX:** dashboard editor + `devai rules lint` CLI.

## Files & modules

```
src/devai/rules/{loader,model,scope,assemble}.py
src/devai/specializations/*            # exists — compose with rules
src/devai/cli/commands.py              # `devai rules lint|show`
dashboard/src/components/rules-panel.tsx
tests/unit/test_rules.py
```

## Config (`DEVAI_*`)

```
DEVAI_RULES_ENABLED=true
DEVAI_RULES_REPO_DIR=.devai/rules
DEVAI_RULES_GLOBAL_DIR=/etc/devai/rules
DEVAI_RULES_MAX_TOKENS=4000
```

## Acceptance criteria

- A repo rule ("use httpx not requests") changes generated code on that repo only.
- Glob‑scoped rule loads only when matching files are touched.
- Exceeding the token budget logs a clear warning and drops lowest‑priority rules.

## Sources

- [Cursor Docs — Rules](https://cursor.com/docs)
- DevAI: `specializations/` (see memory `project_specializations`)
