# Publishing DevAI agents to the Agent Registry

This runbook explains which DevAI publication path to use, how reviewed seed
changes reach production, and how contributors should commit, push, and merge
those changes safely.

The platform publisher's network and GitHub OIDC controls are documented in
the canonical
[Agent Registry publishing runbook](https://github.com/tesserix/tesserix-k8s/blob/main/docs/agent-registry-publishing.md).

## Choose the correct publication path

DevAI has two supported paths. They solve different problems:

| Change | Supported path | Production trigger |
|---|---|---|
| User-authored or tenant-owned Agent | Agent Studio or `devai adk publish` through the authenticated DevAI API | Successful evaluation gate and authorized publish request |
| Built-in DevAI Agent, Skill, Prompt, dataset, evaluation suite, rubric, MCP server, or Project | Reviewed files under `architecture/registry-seeds/` | Merge the DevAI change, then merge a `reseedNonce` bump in `tesserix-k8s` |

Do not publish a tenant Agent directly to the Registry API. Agents pass through
`POST /api/registry/agents`, where DevAI verifies the authenticated owner,
the complete caller-visible reference graph, provider/model fit, static security
policy, risk approval, evaluation run, exact draft, baseline, thresholds, and
any privileged override.
The decision and failure behavior are recorded in
[ADR 0003](../adr/0003-agent-artifact-promotion-gate.md).

## Publish a user-authored Agent

Agent Studio is the normal interactive path. The CLI uses the same authenticated
DevAI API boundary:

```bash
uv run devai adk validate path/to/agent.yaml --deep
uv run devai adk publish path/to/agent.yaml --eval-run-id <owned-run-id>
```

Configure `DEVAI_API_URL` and an authenticated `DEVAI_API_TOKEN` or
`DEVAI_SESSION_COOKIE` through the approved secret-injection mechanism. Do not
pass credentials with `--api-token`, `--session-cookie`, or `--token`: command
arguments can enter shell history and process listings.

For a changed version that intentionally replaces an existing Agent, add
`--overwrite`. A blocked evaluation may only be overridden by an `admin` or
`platform-admin`, with a non-empty `--override-reason`; the server writes the
audit record before publication.

Publication follows one fail-closed sequence:

1. Build resolves all Skill, Tool, MCP server, and Prompt references without
   revealing whether another user's private artifact exists.
2. Security rejects wildcard grants and dangerous prompt instructions. High or
   critical risk requires an audited `admin` or `platform-admin` approval.
3. Test verifies the durable owner-scoped sandbox evaluation when
   `spec.evalSuite` is declared.
4. The server publishes and stamps its own gate evidence. Client-supplied gate
   labels and approval annotations are ignored.

Build and static security failures are not break-glass overrideable. The
dashboard shows their exact actionable findings. Publication means the artifact
is discoverable; only live runtime status may describe it as running.

The CLI must report a successful gate or publication status. A non-zero exit
means nothing should be treated as released.

## Change built-in Registry seeds

### 1. Create or regenerate the artifacts

The source of truth is `architecture/registry-seeds/`. For specialization-backed
Agents, edit `specializations/**/*.yaml` and regenerate:

```bash
make registry-seeds
make registry-seeds-check
```

For a new ADK artifact, scaffold it into the same tree and then edit the
generated YAML:

```bash
uv run devai adk new-agent <agent-name>
uv run devai adk validate architecture/registry-seeds --deep
```

Keep `metadata.namespace: devai` and the naming rules in
[`architecture/registry-seeds/README.md`](../../architecture/registry-seeds/README.md).
Never commit provider API keys, Registry deploy keys, session cookies, test
tokens, or production response payloads.

### 2. Validate the repository

Run the seed guard and the standard backend checks before opening a pull
request:

```bash
make registry-seeds-check
make lint
make type-check
make test
git diff --check
git diff -- architecture/registry-seeds/ specializations/
```

If the change affects only one artifact family, run its smallest relevant test
while iterating, then run the complete commands above before merge.

### 3. Commit and push the DevAI change

Start from current `main` and use a short-lived branch:

```bash
git switch main
git pull --ff-only origin main
git switch -c feat/<short-agent-change>
```

Resolve the remote before committing. The Tesserix identity is:

```bash
git remote get-url origin
git config user.name sam123ben
git config user.email samyak.rout@gmail.com
git add architecture/registry-seeds/ specializations/
git commit -m "feat(registry): publish <artifact-name>"
git push -u origin HEAD
gh pr create --base main --fill
gh pr checks --watch
```

Merge only after required review and CI pass. Do not bypass branch protection,
force-push `main`, or include unrelated generated files:

```bash
gh pr merge --squash --delete-branch
```

Merging DevAI makes the reviewed seeds available to the bootstrap job. It does
not by itself rerun the production bootstrap.

### 4. Request the GitOps reseed

In the existing `tesserix-k8s` repository, update only
`charts/apps/devai-registry-bootstrap/values.yaml` and give `reseedNonce` a new,
meaningful value such as `<date>-<short-purpose>`. Open and merge a normal
GitOps pull request:

```bash
git switch main
git pull --ff-only origin main
git switch -c feat/devai-registry-reseed-<short-purpose>
# Edit reseedNonce, then run the repository's relevant validation.
git add charts/apps/devai-registry-bootstrap/values.yaml
git commit -m "feat(devai): reseed agent registry"
git push -u origin HEAD
gh pr create --base main --fill
gh pr checks --watch
gh pr merge --squash --delete-branch
```

After merge, Argo CD replaces the immutable bootstrap Job. The Job clones
`tesserix/devai@main` and idempotently applies the reviewed seeds in dependency
order. Do not use `kubectl apply`, patch the Job, or publish from a developer
laptop.

Platform maintainers perform production reconciliation and read-only cluster
verification from `tesserix-k8s`; DevAI contributors must follow this
repository's local-sandbox boundary.

### 5. Verify publication

Verify the intended public card after the platform job succeeds:

```bash
curl --fail --silent --show-error \
  "https://aregistry.tesserix.app/v0/agents/<agent-name>/card?namespace=devai"
```

Confirm the returned name, version, image, model, skills, prompts, and MCP
references match the merged manifest. A successful bootstrap does not prove a
specific card is correct unless its content is checked.

## The GitHub OIDC publisher and DevAI

`https://publish-aregistry.tesserix.app/v0/apply` is a machine-only endpoint.
A browser must receive HTTP 403.

At present it authorizes only the manual `publish.yml` workflow in
`tesserix/ai-agents`. The DevAI repository is not authorized to call it, and
copying that workflow here will fail with HTTP 403. DevAI's built-in catalog
continues to use the GitOps bootstrap path above.

If DevAI later needs a repository-driven external publisher, onboarding must be
a reviewed `tesserix-k8s` security change with an exact repository, branch,
event, workflow reference, OIDC audience, tenant, and protected deploy key. Do
not weaken the existing Kora policy or share Kora's deploy key.

## Failure handling

| Symptom | Action |
|---|---|
| `registry-seeds-check` fails | Regenerate the seeds and review the resulting diff; do not hand-edit generated drift away. |
| Agent publication is blocked | Inspect the owned evaluation run, draft hash, suite/dataset versions, thresholds, and baseline. Do not bypass the gate. |
| DevAI PR is merged but the card is unchanged | Confirm the `tesserix-k8s` `reseedNonce` PR was merged and the bootstrap Job completed. |
| Publisher hostname shows `RBAC: access denied` | Expected in a browser and from DevAI, which is not currently authorized on that route. |
| Agent Card returns 404 | Check `metadata.name`, `metadata.namespace`, bootstrap logs, and whether the seed was included in the merged DevAI revision. |

## Rollback

Revert the DevAI seed commit, merge it, then merge another GitOps
`reseedNonce` bump. For a tenant-authored Agent, publish a reviewed replacement
version through the DevAI promotion boundary. Never repair production by
editing the Registry database or applying Kubernetes resources manually.
