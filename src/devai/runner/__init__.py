"""In-pod runner package.

This package is what executes inside a `devai-runner` Job pod. The
entrypoint reads its task metadata from env, resolves the agent's
skills/prompts/mcp servers against aregistry, runs the agent, and
writes a single `RESULT::<json>` line to stdout that the
`JobRunnerStage` parses back into a `StageResult`.

Kept separate from `devai.runtime` (which lives in the api pod) so a
slim runner image doesn't need to import api-side modules like the
PipelineService or the dashboard routes.
"""
