"""Condition expression evaluator for blueprint stages.

Blueprints can guard a stage with `condition: task.has_pr` or
`condition: !task.has_sandbox`. The expression is intentionally tiny —
this is config, not Python — and only resolves to the predicate properties
declared on DevAITask plus a handful of stage-output flags.

Two-tier validation:

  * **Load time** (:func:`validate_condition`) — bare keys (no
    ``output.``/``state.`` prefix) are validated against
    :data:`_TASK_BOOL_KEYS`. An unknown bare key is almost always a typo
    (`task.has_pr` → `task.haspr`) and a typo'd gate would otherwise run
    unconditionally, so we raise at blueprint load and break loudly.
  * **Runtime** (:func:`_lookup`) — prefixed keys (``output.*`` / ``state.*``
    / ``agent_context.*``) stay fail-open for forward-compat with stage
    outputs a newer blueprint may emit, but we WARN when an unknown *bare*
    key falls through so the drift is visible.

Grammar:

    expression  := atom
                 | "!" atom
                 | atom ("and" | "or") atom        # left-associative, no parens
    atom        := identifier
    identifier  := dotted name (e.g. task.has_pr, output.review_decision)
"""

from __future__ import annotations

import logging

from devai.pipeline.types import DevAITask

logger = logging.getLogger(__name__)

# Keys that DevAITask exposes as @property bool. Listed explicitly so a
# typo in the YAML doesn't silently match an unrelated attribute.
_TASK_BOOL_KEYS: frozenset[str] = frozenset(
    {
        "task.has_pr",
        "task.has_issue",
        "task.has_sandbox",
        "task.has_epic",
        "task.has_stories",
        "task.is_terminal",
        "task.is_failed",
    }
)


# Prefixes whose keys are resolved dynamically (stage outputs / task state /
# raw handover bag) and therefore can't be validated at load time. These stay
# fail-open for forward-compat; bare keys do NOT (see validate_condition).
# `agent_context.` is the raw handover-bag name `output.` is sugar for, and
# supports nested dotted access (e.g. agent_context.scan_output.is_blank).
_FAIL_OPEN_PREFIXES: tuple[str, ...] = ("output.", "state.", "agent_context.")


def _resolve_bag_path(bag: object, path: str) -> object:
    """Walk a dotted ``a.b.c`` path through nested mappings; missing → None."""
    cur = bag
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _lookup(task: DevAITask, key: str) -> bool:
    """Resolve a single dotted key against the task.

    Bare ``task.*`` keys are expected to have been validated at blueprint
    load (:func:`validate_condition`); a stale/unvalidated unknown bare key
    still fails open here but emits a WARNING so the drift is visible rather
    than silent. ``output.*`` / ``state.*`` keys are forward-compat and may
    legitimately reference outputs an upgraded blueprint emits.
    """
    # task.* — DevAITask property
    if key in _TASK_BOOL_KEYS:
        attr = key.split(".", 1)[1]
        return bool(getattr(task, attr, False))

    # output.<key> — truthy lookup into agent_context handover bag.
    # Useful for `condition: output.review_decision_approved`, where the
    # ReviewCodeStage wrote `review_decision_approved: True` into its data.
    if key.startswith("output."):
        bag_key = key.split(".", 1)[1]
        return bool(task.agent_context.get(bag_key, False))

    # state.<value> — check whether the task is currently in a specific state.
    if key.startswith("state."):
        wanted = key.split(".", 1)[1]
        return task.state.value == wanted

    # agent_context.<dotted path> — truthy lookup into the raw handover bag,
    # supporting nested access (agent_context.scan_output.is_blank). This is
    # the explicit form of `output.<key>` for nested stage outputs.
    if key.startswith("agent_context."):
        bag_path = key.split(".", 1)[1]
        return bool(_resolve_bag_path(task.agent_context, bag_path))

    # Unknown bare key — should have been caught at load. Keep fail-open so a
    # stale executor doesn't skip work, but WARN (not debug) so it's noticed.
    logger.warning("unknown condition key %r at runtime → fail-open (True)", key)
    return True


# ──────────────────────────────────────────────────────────────────────────
# Load-time validation
# ──────────────────────────────────────────────────────────────────────────


def _condition_keys(condition: str) -> list[str]:
    """Extract the bare identifier tokens from a condition expression.

    Drops the operators (``and`` / ``or``) and any leading ``!``. Used by
    :func:`validate_condition` to check each referenced key.
    """
    keys: list[str] = []
    for tok in condition.split():
        low = tok.lower()
        if low in {"and", "or"} or tok == "!":
            continue
        keys.append(tok[1:] if tok.startswith("!") else tok)
    return keys


def validate_condition(condition: str | None) -> list[str]:
    """Validate a condition's keys at blueprint-load time.

    Returns the list of unknown *bare* keys (no ``output.``/``state.``
    prefix) referenced by the expression — an empty list means the condition
    is structurally usable. Prefixed keys are intentionally not validated
    (they resolve dynamically at runtime). Callers (the blueprint loader)
    raise ``BlueprintLoadError`` when the returned list is non-empty so a
    typo'd gate condition can't silently run unconditionally.
    """
    if not condition or not condition.strip():
        return []
    unknown: list[str] = []
    for key in _condition_keys(condition.strip()):
        if not key:
            continue
        if key in _TASK_BOOL_KEYS:
            continue
        if key.startswith(_FAIL_OPEN_PREFIXES):
            continue
        unknown.append(key)
    return unknown


def evaluate(condition: str | None, task: DevAITask) -> bool:
    """Evaluate a condition string. None / empty → True (always run).

    Supports tiny expression grammar:
        task.has_pr
        !task.has_pr
        task.has_pr and task.has_epic
        task.has_pr or output.review_decision_approved

    Mixing `and` / `or` without parens is left-associative. We intentionally
    do not support parentheses — blueprints stay readable, and complex
    conditions belong inside a stage's own logic, not in YAML.
    """
    if condition is None:
        return True
    expr = condition.strip()
    if not expr:
        return True

    # Tokenize on whitespace
    tokens = expr.split()

    # Simple left-fold parser
    result = _parse_atom(tokens, task)
    i = 1 if not tokens[0].startswith("!") else 2  # consumed atom (1 or 2 tokens)

    while i < len(tokens):
        op = tokens[i].lower()
        if op not in {"and", "or"}:
            raise ValueError(f"unexpected token {op!r} in condition {condition!r}")
        consumed_rhs, value = _parse_atom_at(tokens, i + 1, task)
        if op == "and":
            result = result and value
        else:
            result = result or value
        i += 1 + consumed_rhs
    return result


def _parse_atom(tokens: list[str], task: DevAITask) -> bool:
    """Parse the first atom in the token list."""
    consumed, value = _parse_atom_at(tokens, 0, task)
    if consumed == 0:
        raise ValueError("empty condition")
    return value


def _parse_atom_at(tokens: list[str], start: int, task: DevAITask) -> tuple[int, bool]:
    """Parse one atom starting at `start`. Returns (tokens consumed, value).

    Handles `!atom` (2 tokens) and `atom` (1 token). The `!` may also be
    glued: `!task.has_pr` (1 token starting with `!`).
    """
    if start >= len(tokens):
        return 0, False

    head = tokens[start]
    if head == "!":
        # Separate ! token; consume the next as the atom
        if start + 1 >= len(tokens):
            raise ValueError("dangling '!' in condition")
        return 2, not _lookup(task, tokens[start + 1])

    if head.startswith("!"):
        # Glued: !task.has_pr
        return 1, not _lookup(task, head[1:])

    return 1, _lookup(task, head)


__all__: list[str] = ["evaluate", "validate_condition"]
