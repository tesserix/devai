"""Stage implementations.

Three families:

    lifecycle.py — deterministic stages (no agent calls): create_issue,
                   cleanup, post_report, context_hydration, memory_injection,
                   detect_pr, await_merge, noop.

    alm.py       — 14 ALM agent adapters that wrap the existing classes in
                   `devai.agents/` and translate DevAITask ↔ ALMState.

    sre.py       — 7 SRE agent adapters wrapping `devai.sre.agents/`.

Every stage factory has signature:

    def my_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage

…and is registered in `devai.blueprint.registry.register_defaults`.

Adapter contract:
    Stages SHOULD tolerate missing deps (None SCMClient, None StateManager)
    so unit tests can exercise the blueprint runtime without the full
    runtime infrastructure. When a dep is missing, the stage should
    short-circuit with a clear log line and a StageResult that doesn't
    block downstream work (use stub data rather than raising).
"""
