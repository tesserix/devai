# DevAI

Developing an external Agent? Follow [Bring your own Agent to DevAI](docs/guides/bring-your-own-agent.md) for the signed Registry → immutable import → sandbox → evaluation → comparison → gated promotion journey.

DevAI is an agentic application-lifecycle and SRE platform. It combines versioned
agents, prompts, skills, MCP tools, isolated sandboxes, evaluation gates, workflow
orchestration, traces, and owner-attributed model usage in one control plane.

## Start here

- [Install and authenticate the DevAI CLI](docs/guides/install-and-authenticate.md) —
  install or upgrade the macOS CLI with Homebrew and create a secure session.
- [Feedback and support](docs/guides/feedback-and-support.md) — create a request,
  continue the conversation, and track it until support resolves it.
- [Sandbox and evaluation concepts](docs/concepts/sandbox-and-evals.md) — learn the
  lifecycle from an agent draft to a compared and promoted version.
- [Platform architecture](docs/PLATFORM-ARCHITECTURE.md) — services, runtime paths,
  data flow, and deployment boundaries.
- [Agent Registry publishing](docs/agentic/AGENT-REGISTRY-PUBLISHING.md) — publish
  user-owned and built-in artifacts through the correct gate.
- [Agentic integration](docs/agentic/AGENTIC-INTEGRATION.md) — AgentGateway,
  Agent Registry, MCP Hub, and supporting services.

## Install the CLI

The supported Homebrew formula provides native Apple Silicon and Intel macOS
binaries:

```bash
brew tap tesserix/tap
brew install devai
devai auth login --api-url https://devai.tesserix.app
devai auth status
```

Use `brew update && brew upgrade devai` to install the latest release. See the
[installation and authentication guide](docs/guides/install-and-authenticate.md)
for keychain behavior, verification, upgrades, and command-shadowing fixes.

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
