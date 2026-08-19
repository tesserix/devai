# DevAI registry seeds

In-tree v1alpha1 manifests for every Skill / Agent / Prompt / MCPServer that
DevAI publishes to the Solo.io agentregistry (aregistry). These seeds are
the **source of truth** — when the cluster is bootstrapped or rebuilt, the
`devai-registry-bootstrap` ArgoCD Job applies every YAML under this tree
against the live aregistry's v0 API.

Mirrors Fiber's pattern in `architecture/registry-seeds/` (PLATFORM.md §13).

## Layout

```
architecture/registry-seeds/
  skills/              one per Skill — derived from specializations/*/  *.yaml
  agents/              one per Agent — runtime resource that points at a Skill
  prompts/             one per Prompt — extracted system prompts so the
                       dashboard prompt-editor can A/B test variants
  datasets/            versioned public golden cases for built-in agents
  eval-suites/         versioned scorer and threshold gates over datasets
  mcp-servers/         one per MCP server DevAI publishes (fiber-mcp etc.)
  projects/            top-level Project resource grouping it all
```

## CR shape

Every file conforms to:

```yaml
apiVersion: registry.solo.io/v1alpha1
kind: {Skill,Agent,Prompt,MCPServer,Project}
metadata:
  name: <kebab-case-name>
  namespace: devai              # always "devai" — the aregistry partition
  labels:
    devai.io/source: devai      # exclusive marker for tesserix-managed seeds
    devai.io/category: <category>
spec:
  ...
```

## Naming convention

- Skill: `<spec-name>` — matches the YAML in `specializations/` (e.g. `senior-developer`)
- Agent: `<spec-name>-agent` — the runtime that delivers the skill
- Prompt: `<spec-name>-prompt-v<n>` — versioned; bump n when prompt changes
- MCPServer: `<server-name>-mcp` (e.g. `fiber-mcp`, `sre-mcp`)
- Project: `devai` — single project for the whole platform

## Regenerating

Edit `specializations/**/*.yaml`, then:

```bash
make registry-seeds        # regenerates skills/, agents/, prompts/ from specs
make registry-seeds-check  # validates structure + naming
```

A CI guard runs `registry-seeds-check` on every PR to prevent drift.

## Loading into the cluster

`devai-registry-bootstrap` (an ArgoCD app pointing at
`tesserix-k8s/charts/apps/devai-registry-bootstrap`) runs a Kubernetes Job
that POSTs every YAML under this tree to the aregistry v0 API. The Job is
idempotent — re-syncing the ArgoCD app re-applies everything; aregistry
upserts by `(kind, name, namespace)`.
