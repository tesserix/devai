# ADR-0002: Versioned evaluation storage and durability

Status: accepted (application-side contract)

## Context and planning envelope

Evaluation comparisons are meaningful only when every run names the exact immutable
dataset and suite versions it consumed. Dataset cases can contain adversarial prompts,
tool expectations, fixtures, and expected outputs, so they are user-owned content and
must never be discoverable through another principal's scope.

Until production measurements replace these assumptions, size this path for 10 peak
evaluation API requests/s, at most 50 cases/run, and approximately 4 KiB/case. Expect
roughly 10,000 dataset versions and 2 GiB of case blobs/year. Retain dataset metadata,
suite metadata, eval runs, and case results for three years. Metadata reads should stay
below 300 ms p99, excluding object-store download time.

## Decision

Keep evaluation metadata and durable run history in the existing PostgreSQL database.
Store each dataset version's cases as canonical JSON in the existing object-store
adapter, addressed by its SHA-256 digest. Dataset names are unique only within the
server-derived `Principal.user_scope_id`; version, description, case count, content
hash, and blob key are immutable version metadata. Different users may independently
reference identical content hashes, but every metadata lookup includes `owner_scope`
in SQL and foreign objects read as not found.

An eval suite pins one dataset-version foreign key and records its scorer list and
structured thresholds. An eval run stores the exact dataset-version and suite foreign
keys plus all case results in one PostgreSQL transaction. Runs deliberately store a
plain sandbox identifier rather than a sandbox foreign key, so sandbox destruction
cannot cascade into evaluation history.

Each run also snapshots the immutable sandbox configuration used for the run.
Registry-backed built-in definitions retain their exact dataset and suite references
when there is deliberately no user-owned foreign-key row. Comparisons persist two
owned run identifiers, the requested axes, and the computed result in PostgreSQL.
Both runs must belong to the authenticated `owner_scope` and reference the same
immutable dataset version. The comparison identifier is a deterministic digest of
owner, run pair, and axes, so retried creates are idempotent without another
coordination system. Comparison rows restrict deletion of referenced run history and
never cascade from sandbox lifecycle.

The create path uploads the content-addressed object before committing its metadata.
A crash or database failure can therefore leave a harmless unreferenced blob; it can
never leave a committed dataset pointing at content that was not uploaded. A later
create of the same canonical content safely reuses the blob.

Do not add another datastore, service, queue, or sharding layer at this envelope. The
current PostgreSQL and object-store adapters are sufficient and keep ownership checks
inside the existing authenticated control plane.

## Consistency and failure behavior

- PostgreSQL and object-store failures return 503 for dataset/suite operations.
- Durable eval-run writes and reads fail closed; they never degrade to an empty or
  successful response when PostgreSQL is configured.
- Registry publishing and discovery are degradable. A registry outage does not weaken
  ownership checks or make durable evaluation data unavailable.
- Test-only in-memory/Redis stores remain available only when no database is injected;
  production wiring creates the eval runner only with PostgreSQL.
- API responses omit `owner_scope` and internal blob keys. Ownership, tenant, user, and
  role values are never accepted from request bodies.

## Retention and operations

Retain durable rows and referenced blobs for three years. A future cleanup job must
first prove that no retained dataset version references a blob before deleting it.
Deletion is outside this change because it needs an operator-approved retention policy
and recoverability procedure. Measure version creation rate, blob bytes, metadata p99,
PostgreSQL errors, and object-store errors before changing the planning envelope.

## Alternatives rejected

- Storing cases directly in PostgreSQL: rejected because large immutable payloads make
  metadata indexes and backups unnecessarily heavy.
- Keying ownership by email, user ID, or client-supplied tenant: rejected because those
  values are not globally safe ownership boundaries.
- Cascading eval history from sandboxes: rejected because ephemeral workload cleanup
  must not delete comparison, audit, or cost evidence.
- Per-tenant databases or buckets: rejected because current scale does not justify the
  operational and connection-pool cost; scoped SQL provides the required isolation.
