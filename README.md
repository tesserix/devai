# DevAI

Developing an external Agent? Follow [Bring your own Agent to DevAI](docs/guides/bring-your-own-agent.md) for the signed Registry → immutable import → sandbox → evaluation → comparison → gated promotion journey.

DevAI is an agentic application-lifecycle and SRE platform. It combines versioned
agents, prompts, skills, MCP tools, isolated sandboxes, evaluation gates, workflow
orchestration, traces, and owner-attributed model usage in one control plane.

## Start here

- [Sandbox and evaluation concepts](docs/concepts/sandbox-and-evals.md) — learn the
  lifecycle from an agent draft to a compared and promoted version.
- [Platform architecture](docs/PLATFORM-ARCHITECTURE.md) — services, runtime paths,
  data flow, and deployment boundaries.
- [Agent Registry publishing](docs/agentic/AGENT-REGISTRY-PUBLISHING.md) — publish
  user-owned and built-in artifacts through the correct gate.
- [Agentic integration](docs/agentic/AGENTIC-INTEGRATION.md) — AgentGateway,
  Agent Registry, MCP Hub, and supporting services.

## Local development

```bash
uv sync --all-extras
uv run pytest
uv run devai --help
```

The dashboard lives under `dashboard/` and uses its own Node.js dependencies:

```bash
cd dashboard
npm install
npm run dev
```

Configuration uses `DEVAI_*` environment variables. Never commit provider keys,
session cookies, Registry tokens, or Kubernetes credentials.
