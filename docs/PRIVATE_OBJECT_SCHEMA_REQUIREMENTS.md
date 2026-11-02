# Private object schema and rollout contract

Revision `d9f1a3c5e7b2` creates the registry, deletion outbox, initial nullable
resource references, composite tenant foreign keys, forced RLS policies, and
the fixed-search-path deletion claim primitive described below. Revision
`e1b5d7f9a203` extends the same contract to interview recordings. New candidate,
assessment, task, and recording uploads dual-write the registry and
`private_object_id`. Revision `e2c6f8a0b314` adds durable verified-legacy
deletion and tenant reconciliation cursors.
The legacy `candidates.pdf_path`, `assessment_files.storage_path`,
`task_submissions.file_path`, and `interview_recordings.media_path` columns
remain as compatibility columns for one release after the operator-controlled
backfill and reconciliation; migrated resources now store canonical private
keys in those columns and authoritative ownership in `private_object_id`.

The operator cutover is implemented as a bounded, resumable service command,
not as an Alembic data migration. It globally preflights normalized legacy-key
ownership under a PostgreSQL advisory fence, validates and scans each file,
then commits one resource binding, registry row, usage delta, and digest-bound
legacy deletion obligation as one database unit. Candidate merge and reversal
take the exclusive side of that fence before locking candidates. This prevents
the preflight estate from changing through the supported legacy-reference
writer while a cutover is active. The preserved-tenant 2026-08-24 apply,
reconciliation, backup/restore, authorized-read smoke, and worker drain are
complete: 62/62 references became ready canonical objects and 62/62 verified
redundant sources drained without failure. Seven opaque unreferenced legacy
files remain outside the resource backfill and were neither adopted nor
deleted. Exact evidence is recorded in
`docs/evidence/preserved-data-legacy-cutover-e18-2026-08-24.json`.

## `private_objects`

Landed columns:

- `id uuid primary key`
- `organization_id uuid not null`
- `category varchar(32) not null` with a reviewed allowlist containing
  `candidates`, `assessments`, `tasks`, `ai`, and `recordings`; the dedicated
  `ai` and `recordings` categories preserve distinct provenance and operational
  policy
- `object_key text not null unique` (opaque; `private/<org>/<category>/<uuid>.<ext>`)
- `original_filename varchar(255) not null`
- `media_type varchar(255) not null`
- `size_bytes integer not null check (size_bytes between 1 and 15728640)`
- `sha256 char(64) not null` with a lowercase-hex check
- `state varchar(24) not null` checked to `quarantined`, `ready`, `rejected`,
  `delete_pending`, or `deleted`
- `scanner_engine`, `scanner_version`, and `scanner_verdict` as bounded strings
- `resource_type varchar(32) not null` and `resource_id uuid not null` for
  reconciliation lookup; service code, not a polymorphic database FK, assigns
  these from the authenticated resource
- `retention_until`, `legal_hold_at`, `ready_at`, `rejected_at`, `deleted_at`,
  `created_at`, and `updated_at` as timezone-aware timestamps

Constraints/indexes:

- unique `(organization_id, id)` for composite tenant foreign keys
- unique `(organization_id, object_key)` in addition to global key uniqueness
- index `(organization_id, resource_type, resource_id)`
- index `(state, retention_until)` where state is `ready` or `delete_pending`
- a fixed-search-path trigger/check that verifies the organization and category
  segments embedded in `object_key`; callers may not supply those segments
- forced RLS using the existing trusted database tenant context

Nullable `private_object_id uuid` is present on `candidates`,
`assessment_files`, `task_submissions`, and `interview_recordings`, with
composite foreign keys
`(organization_id, private_object_id) -> private_objects(organization_id, id)`.
The preserved-data backfill accepted only references resolving under the
approved legacy root with verified hash/size; all 62 referenced resources
passed. Future or tenant-specific backfills retain the same fail-closed rule:
quarantine invalid/cross-tenant references for manual review and never copy by
trusting stored path text. After the one-release compatibility window and a
full retention cycle pass, make assessment/task references required when their
status/type says a file exists, and retire raw path semantics.

AI-session attachments are represented only in `private_objects` with
`category='ai'`, `resource_type='ai_attachment'`, and `resource_id` bound to the
owning `ai_sessions.id`. They expire after 24 hours and are resolved by an
exact tenant/session/registry-ID lookup. The polymorphic `resource_id` cannot
carry a conventional foreign key; reconciliation performs the tenant-scoped
owner check and fails closed on unknown resource types.

## `private_object_deletions`

Landed columns:

- `id uuid primary key`, `organization_id uuid not null`, and
  `private_object_id uuid not null`
- `object_key_snapshot text not null` and `sha256_snapshot char(64) not null`
- allowlisted `reason`, `requested_at`, `not_before`, `attempt_count`,
  `claimed_at`, `claim_token`, `next_attempt_at`, `completed_at`, and a bounded
  `last_error_code` that never stores provider bodies
- composite FK `(organization_id, private_object_id)` to `private_objects`
- unique active deletion per object (partial unique index where
  `completed_at is null`), claim index `(completed_at, next_attempt_at)`, and
  forced tenant RLS

The API transaction changes the registry to `delete_pending` and inserts the
outbox row. A global durable worker claims only row ID plus organization via the
fixed-search-path security-definer claim primitive, binds trusted tenant
context, deletes the object idempotently, then marks both rows complete. This
closes the object-store/database transaction gap and works without
`BYPASSRLS`.

`deleted` rows are durable pseudonymous storage tombstones, not an excuse to
retain data-subject linkage. Candidate erasure inventories matching registry
rows in every lifecycle state. Only `ready`/`delete_pending` objects participate
in legal-hold and physical-delete decisions, but every matching row has
`resource_id`, original filename, media type, and scanner-identifying metadata
scrubbed before the subject transaction commits.

## Legacy deletion and reconciliation progress

Revision `e2c6f8a0b314` lands `legacy_private_object_deletions` for truly
pre-registry local paths (`cvs/`, `assessment_files/<org>/`,
`task_files/<org>/`, and `recordings/<org>/`). Each row contains tenant,
allowlisted category/resource binding, normalized relative key, size and
SHA-256 snapshot, bounded retry/claim state, and error code. The normalized key
is globally unique so one physical path cannot be rebound across tenants.
Exact root, UUID filename, extension, size, and digest checks run both at
enqueue and delete. Forced RLS and the fixed-search-path claim primitive expose
only row/tenant/lease identity. Resource replacement/deletion and enterprise
retention insert this outbox row before committing their mutation; storage
success followed by a crash is safe because a missing file settles
idempotently on retry.

Local settlement uses an atomic same-directory claim rather than a
check-then-unlink sequence. The worker renames the source to an
outbox-ID-derived tombstone without following links, hashes the opened inode,
and deletes only that claimed inode when all recorded identity fields still
match. If another writer recreates the original name or any check fails, the
worker preserves both the replacement and the claimed bytes, keeps the outbox
unsettled, and exposes a bounded error for operator review. An existing crash
tombstone is a resumable claim.

Retention may encounter a `replacement` row created by cutover after the
resource already points at canonical storage. Before scrubbing the subject it
locks and revalidates the outstanding legacy copy, changes the obligation to
`candidate_erasure`, accelerates it, clears stale claims/errors, and resets a
dead-lettered retry count. It never updates a deletion digest to adopt changed
bytes. Absence remains an idempotent success; a mismatch rolls the complete
retention unit back.

The same revision lands `private_storage_reconciliation_cursors` with composite
primary key `(organization_id, namespace)`, where namespace is `registry`,
`private`, or `quarantine`. Its canonical key, cycle count, and completion time
are tenant-RLS protected. Applying scans lock and advance progress in the same
transaction as registry changes. Dry runs never create or advance it. S3 uses
canonical-key `StartAfter`, not a durable provider token, and wraps at the end
so arbitrarily large namespaces and later lexicographically earlier objects
are eventually covered.

Owned interview media now uses the reviewed `recordings` category and exact
`resource_type='interview_recording'` registry binding. External `media_url`
remains owned by its integration provider and requires provider-specific
deletion orchestration; it is not represented as an owned blob.

Candidate merge treats a registered CV as one owned registry object rather
than copyable path text. If the primary has no document, merge moves the alias
`pdf_path` and `private_object_id`, changes the registry `resource_id` to the
primary, and clears the alias. Distinct alias documents keep exact ownership;
repeated legacy path aliases are cleared so one local blob never has multiple
resource owners. Encrypted merge snapshots include the affected registry rows,
and reversal restores the exact candidate/object binding only if the current
graph still matches the post-merge digest. Cross-tenant, mismatched-key, and
unregistered private-shaped references fail closed.

AI imports also need durable provenance if candidate erasure must remove the
source attachment before its 24-hour expiry. Add a tenant-owned association
from candidate to source `private_object_id` (composite tenant foreign keys on
both sides and a unique candidate/object pair). Retention can then include that
attachment while still preserving a shared source until its final candidate is
erased.
