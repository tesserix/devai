# Bring your own Agent to DevAI

This flow is for developing an Agent in the Tesserix ADK, LangGraph, Google ADK, OpenAI Agents SDK, A2A, or a custom OCI runtime. DevAI imports and tests the exact Registry version; it does not become an unrestricted always-on automation runner.

## 1. Export a portable Agent

Tesserix ADK users can emit the portable Registry manifest directly. The framework adapters inspect public framework attributes and do not install those frameworks into the DevAI API image.

```python
from tesserix_adk.adapters.portable import container_runtime, export_langgraph_agent

manifest = export_langgraph_agent(
    graph,
    name="support-agent",
    namespace="acme-ai",
    version="1.2.0",
    runtime=container_runtime(
        image="ghcr.io/acme/support-agent@sha256:<64-hex-digest>",
        path="/a2a/v1",
        health_path="/readyz",
    ),
)
```

Google ADK and OpenAI Agents SDK use `export_google_adk_agent` and `export_openai_agent`. An authenticated remote Agent Card uses `export_a2a_agent` plus `remote_runtime`; a custom image uses `export_oci_agent`. Remote runtimes can become `callable`, but DevAI must not describe them as isolated or reproducible.

Every container image and Registry dependency must use an immutable digest/version. Manifests contain secret references such as `openbao://...`, never credential values.

## 2. Sign in and publish to Agent Registry

```bash
agentic auth login --registry https://aregistry.tesserix.app
agentic validate -f agent.yaml --resolve
agentic apply -f agent.yaml
```

For CI, create a least-privilege credential on the Registry **API Credentials** page and store these values in the CI secret/config store:

```text
AGENTIC_CLIENT_ID       secret-store reference or protected variable
AGENTIC_CLIENT_SECRET   secret
AGENTIC_TOKEN_URL       non-secret configuration
AGENTIC_AUDIENCE        non-secret configuration
```

The CLI exchanges the client secret for a short-lived token. Do not pass the Registry client secret to DevAI. Rotation creates an overlap credential; revocation removes the machine secret so it cannot obtain another access token.

Save the returned canonical reference, digest, signature identity, and dependency lock:

```text
registry://acme/agents/acme-ai/support-agent@1.2.0
```

## 3. Import the exact version into a DevAI project

Authenticate to DevAI separately, then search or paste the exact reference:

```bash
export DEVAI_API_URL=https://api.devai.tesserix.app
export DEVAI_API_TOKEN='<short-lived DevAI token>'

devai adk registry search "support ticket triage" --kind Agent
devai adk registry import \
  registry://acme/agents/acme-ai/support-agent@1.2.0 \
  --project support-lab \
  --idempotency-key import-support-1 \
  --output json
```

DevAI verifies the Registry signature, resolves every caller-visible dependency to an exact version/digest, computes conformance findings, and stores an immutable project-owned import. A later Registry outage does not rewrite the stored lock.

## 4. Create a safe sandbox and inspect a trace

Use the import ID returned above. `mock`, `replay`, or `block` is the safe default; `real` requires explicit permission and policy approval.

```bash
devai adk sandbox from-import <import-id> --tool-mode mock --output json
devai adk sandbox wait <sandbox-id>
devai adk sandbox invoke <sandbox-id> "Resolve ticket 4471"
devai adk sandbox traces <sandbox-id>
```

The stored sandbox snapshot pins the Agent digest, dependency lock, runtime, model configuration, prompt, tool modes, permissions, and dataset. Sandboxes inherit no Registry, platform, cloud, or production credentials.

## 5. Evaluate, fix, compare, and promote

Publish immutable Dataset and EvalSuite dependencies, then run the pinned suite:

```bash
devai adk test support-agent \
  --sandbox-id <sandbox-id> \
  --suite evals/release-gate.yaml \
  --output json
```

Use the failing case's `trace_url` to inspect model/tool/MCP causality. Export and publish a fixed Agent version, import it with a new request key, and evaluate its new sandbox against the same suite and dataset versions.

```bash
devai adk compare <baseline-run-id> <candidate-run-id> --output json
devai adk publish agent-1.2.1.yaml \
  --api-url "$DEVAI_API_URL" \
  --api-token "$DEVAI_API_TOKEN" \
  --eval-run-id <candidate-run-id>
```

Direct Registry publication makes an artifact discoverable. Only the DevAI promotion boundary can attach DevAI-owned build, security, evaluation, comparison, and approval evidence for the exact candidate.

Finally, tear down the sandbox. TTL cleanup remains a safety net, not the normal lifecycle.

```bash
devai adk sandbox destroy <sandbox-id>
```

## Failure behavior

- A duplicate request key returns the same import, sandbox, or evaluation; reusing it with different input is rejected.
- Registry/signature/dependency failures block import. A mutable image tag never falls back to a local image.
- Judge/scoring outages preserve Agent invocations and retry scoring without rerunning real tools.
- A failure after a real tool may have executed is non-retryable and requires explicit side-effect review.
- Cancellation, timeout, cleanup backlog, and stuck workflows remain queryable and alertable.
- Another tenant receives `404` for private imports, sandboxes, runs, traces, comparisons, approvals, and secret references.
