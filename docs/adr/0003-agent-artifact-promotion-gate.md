# ADR 0003: Gate agent artifacts at the registry publication boundary

- Status: accepted
- Date: 2026-08-19
- Issue: #190

## Context

DevAI has two different things called promotion:

1. Kargo promotes immutable DevAI application container images from the `deploy`
   branch into the production Argo CD stage.
2. A user promotes an Agent definition from an unpublished sandbox draft into the
   agent registry, where runtimes can discover it immediately.

Only the second operation changes an agent artifact. Blocking application-image
freight based on one user's agent evaluation would couple every tenant to an unrelated
platform release and create a platform-wide denial-of-service path.

The sizing envelope for this control-plane path is 20 agent publishes/minute at peak,
manifests no larger than the existing 256 KiB authoring limit, and roughly 10 reads per
write. At 36 months, gate metadata remains well below one million rows because the
durable evaluation run and comparison stores retain the evidence while the registry
stores only identifiers. The target is 99.9% monthly availability and p99 below five
seconds for publication after an evaluation already exists. Evaluation execution is a
separate asynchronous workload and is not inside that latency budget.

Assets worth protecting are tenant-owned agent definitions, evaluation evidence,
provider spend, and privileged override authority. Threat actors include an
authenticated user attempting cross-tenant run reuse, a user replaying a stale run for a
changed draft, and a non-admin attempting to forge approval metadata. The trust boundary
is the authenticated DevAI registry API; identity and ownership are derived there and
never accepted from the manifest.

## Decision

`POST /api/registry/agents` is the agent-artifact promotion boundary.

- An Agent that declares `spec.evalSuite` must present a durable evaluation run ID.
- The server looks up that run by the verified principal's owner scope and verifies the
  exact Agent spec, suite version, and dataset version.
- New versions compare against the server-stamped evaluation run on the currently
  published version. A missing published baseline fails closed.
- Absolute threshold failures and baseline regressions block publication and return the
  exact cases and metrics.
- Only `admin` or `platform-admin` may override a blocked gate. A non-empty reason and a
  successful append-only audit write are required before the artifact is published.
- Gate, lifecycle, run, comparison, approver, and override metadata are server-stamped;
  client-supplied values in that namespace are removed.
- Agent Studio and `devai adk publish` use this authenticated API. The direct registry
  client remains for trusted bootstrap and non-Agent catalog artifacts inside the
  platform network; it is not a supported user Agent publication path.
- The current Kargo application-image train is unchanged. If a distinct Kargo warehouse
  for Agent artifacts is introduced later, its promotion step must call this same DevAI
  decision boundary rather than reimplementing gate logic.

This route supplies the Test decision required by the planned #75 agent lifecycle
harness. Build and Security remain separate #75 stages; they do not weaken or bypass this
evaluation decision.

## Failure behavior

- Evaluation storage unavailable: `503`; no publication.
- Candidate run missing or owned by another principal: blocked as an unavailable owned
  run; no cross-tenant existence signal.
- Registry unavailable: `502`; no publication.
- Comparison unavailable or invalid: fail closed; no publication.
- Audit storage unavailable during override: `503`; no publication.
- Retried successful publication follows the registry's existing version/idempotency
  behavior. Override audit is written before registry publication, so a registry failure
  can leave an audit attempt without an artifact but can never leave an overridden
  artifact without an audit record.

## Alternatives considered

- Gate the existing Kargo DevAI image Stage: rejected because its freight is an
  application image, not a tenant Agent artifact.
- Trust a client-supplied `passed` annotation: rejected because it is forgeable and does
  not prove run ownership or draft equality.
- Put the policy only in Agent Studio: rejected because CLI and direct HTTP callers would
  bypass it.
- Introduce another service or datastore: rejected because the modular application,
  existing Postgres evaluation store, registry proxy, and audit log meet the load and
  consistency requirements.

## Consequences

Publication adds one owner-scoped run lookup and, for an update, one stored comparison.
The expected incremental infrastructure cost is negligible relative to the evaluation
itself. A storage or registry outage deliberately reduces availability of gated
publication rather than allowing an unverified release.

The change is additive: Agents without `spec.evalSuite` retain their prior behavior.
Rollback is one application-image rollback; no schema contraction or data migration is
required. Server-stamped annotations remain harmless to the previous version.
