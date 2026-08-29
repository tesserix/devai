"""A failed stage must report why, not just that it failed.

A stage that raises reaches the workflow wrapped in Temporal's ActivityError,
whose message is the constant "Activity task failed". Recording that verbatim
left runs like the LLM-outage one below saying nothing an operator could act on.
"""

from devai.orchestration.workflows import root_cause


def _chain(*messages: str) -> Exception:
    """Build an exception chain outermost-first, linked by __cause__."""
    exc = Exception(messages[-1])
    for message in reversed(messages[:-1]):
        outer = Exception(message)
        outer.__cause__ = exc
        exc = outer
    return exc


def test_the_innermost_reason_wins_over_the_temporal_wrapper() -> None:
    failure = _chain("Activity task failed", "all authorized LLM providers failed")

    assert root_cause(failure) == "all authorized LLM providers failed"


def test_a_blank_inner_message_falls_back_to_the_last_useful_one() -> None:
    failure = _chain("Activity task failed", "anthropic: model not found", "")

    assert root_cause(failure) == "anthropic: model not found"


def test_an_unchained_exception_reports_itself() -> None:
    assert root_cause(RuntimeError("crew catalog is empty")) == "crew catalog is empty"


def test_a_self_referential_chain_terminates() -> None:
    looped = Exception("outer")
    inner = Exception("inner")
    looped.__cause__ = inner
    inner.__cause__ = looped

    assert root_cause(looped) == "inner"
