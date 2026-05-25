"""Condition expression evaluator for blueprint stages.

Blueprints can guard a stage with `condition: task.has_pr` or
`condition: !task.has_sandbox`. The expression is intentionally tiny —
this is config, not Python — and only resolves to the predicate properties
declared on DevAITask plus a handful of stage-output flags.

Fail-open semantics: unknown keys evaluate to True so a stale executor
running an upgraded blueprint doesn't silently skip work. This matches
the Fiber reference.

Grammar:

    expression  := atom
                 | "!" atom
                 | atom ("and" | "or") atom        # left-associative, no parens
    atom        := identifier
    identifier  := dotted name (e.g. task.has_pr, output.review_decision)
"""

from __future__ import annotations

import logging
from typing import Any

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


def _lookup(task: DevAITask, key: str) -> bool:
    """Resolve a single dotted key against the task. Fail-open."""
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

    logger.debug("unknown condition key %r → fail-open (True)", key)
    return True


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


__all__: list[str] = ["evaluate"]
