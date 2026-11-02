# Private-Pilot Operations Runbook

This runbook covers manual tenant provisioning, release topology verification,
health and alert triage, rollback, and offboarding. It supplements the detailed
[`RECOVERY_RUNBOOK.md`](./RECOVERY_RUNBOOK.md),
[`INTEGRATION_RUNTIME.md`](./INTEGRATION_RUNTIME.md), and
[`../ops/observability/README.md`](../ops/observability/README.md).

## Operating boundaries

- Provision organizations only from a reviewed contract. There is no public
  signup, checkout, or implicit free tier.
- Keep API, worker, PostgreSQL, Valkey, MinIO, ClamAV, and monitoring networks
  private. TLS terminates at the deployment ingress; the reference Compose
  topology uses local HTTP only.
- Use independent secrets for Auth.js, API sessions, assessment sessions, JWT,
  MFA, integration encryption, public rate limiting, PostgreSQL roles, MinIO
  operator access, and MinIO application access.
- Never run the application with a PostgreSQL owner/superuser login. The API
  and worker use their separate non-owner roles with forced tenant RLS.
- Treat `/enterprise/backup` as a portability export, not a recovery backup.
  Recovery uses the encrypted database-plus-private-object workflow.

## Provision a tenant

Start from [`../ops/provisioning/tenant-contract.example.json`](../ops/provisioning/tenant-contract.example.json).
Every entitlement metric is mandatory: `null` is unlimited, `0` is blocked,
and a positive integer is a hard cap in the named period. AI cost is measured
in micro-USD and storage in bytes. Retention must be 30–730 days.

Use an operator database connection and an absolute invitation-output path on
encrypted storage. The dry run validates the complete contract and confirms
that the slug and first-admin email are unused without creating a tenant:

```bash
cd apps/api
uv run python -m app.scripts.provision_tenant \
  --contract /secure/contracts/customer.json \
  --confirm-slug customer-slug \
  --dry-run
```

Apply the reviewed contract:

```bash
uv run python -m app.scripts.provision_tenant \
  --contract /secure/contracts/customer.json \
  --confirm-slug customer-slug \
  --invitation-output /secure/handoff/customer-first-admin.invite
```

The transaction creates the organization, explicit entitlement rows, bounded
retention policy, inactive first admin, hashed seven-day invitation, and audit
event. The raw invitation URL is written once to a new mode-0600 file; it never
appears in stdout or the database. Transfer it through the approved secret
channel, require the first admin to set a password and enroll TOTP MFA, then
confirm the invitation is consumed. Revoke and reissue it if delivery is
uncertain. The legacy `seed_admin` command is development-only and requires an
explicit unsafe-development confirmation; it is not a pilot provisioning path.

After activation, verify the organization name/branding, the complete
`/enterprise/entitlements` snapshot, retention, RU/EN locale, audit event, and
that a second admin can be invited before any personnel change affecting the
first admin.

## Deploy and verify the reference topology

Use a unique Compose project name. Fill every value in `.env.compose.example`
with independent secrets; never reuse a developer `.env` file.

```bash
docker compose \
  --project-name hr-agent-release-gate \
  -f docker-compose.dev.yml \
  -f ops/docker-compose.release-gate.yml \
  config --quiet

docker compose \
  --project-name hr-agent-release-gate \
  -f docker-compose.dev.yml \
  -f ops/docker-compose.release-gate.yml \
  up --detach --build --scale api=2 --scale worker=2
```

The isolation overlay publishes no host ports. `migrate` and `minio-init` must
finish successfully at the expected release head (E18:
`e18c4f6a0b32`); PostgreSQL, Valkey, MinIO, and ClamAV must be healthy;
exactly two API and two worker replicas must be running. From an API replica,
request `http://api:8000/health/live` and `http://api:8000/health/ready`.
Public readiness must report only `status` and `checked_at`, include
`Cache-Control: no-store`, and contain no component, migration, role, queue,
storage, or worker detail. With an authenticated operator identity holding
`enterprise.manage`, request `/enterprise/operations/readiness`; that response
must report successful database, migration, forced-RLS, private-storage,
malware-scanner, distributed-rate-limit, queue-age, and fresh-worker coverage
components without credentials or connection strings.

The reference scanner is the official digest-pinned ClamAV Alpine LTS 1.4.6
image. Readiness must prove a live clean scan and a parseable signature version,
and must fail closed when signature age exceeds 48 hours. The alert threshold is
the same fixed 48 hours; do not make one threshold independently configurable.

Run the golden hiring smoke through the web service and confirm a durable test
job reaches one terminal success exactly once. Stop one API replica and one
worker replica, repeat readiness and a local mutation, then recreate the two
replicas. A stale lease must not acknowledge work, and the same idempotency key
must not duplicate an external effect. Inspect provider/job health and replay
only through authenticated operator controls—never by editing queue tables.

Stop the isolated gate without deleting its named volumes:

```bash
docker compose \
  --project-name hr-agent-release-gate \
  -f docker-compose.dev.yml \
  -f ops/docker-compose.release-gate.yml \
  down
```

Volume deletion is a separate, explicitly approved action after evidence and
recovery artifacts have been retained.

## Health, metrics, and alerts

- `/health/live` proves only that the HTTP process can respond.
- `/health/ready` is authoritative for traffic admission and fails closed for
  migration drift, tenant-RLS failure, storage/scanner failure, unavailable
  distributed abuse protection, stale queue age, or missing worker coverage,
  but returns only the redacted public readiness schema.
- `/enterprise/operations/readiness` is the operator diagnostic view. It uses
  the same single-flight snapshot as public readiness and requires
  `enterprise.manage`; do not expose it through public ingress or monitoring
  credentials with broader application access.
- `/internal/metrics` remains disabled or private and independently
  authenticated. It must never be exposed by the web BFF or public ingress.
- Alert thresholds and triage steps are defined in
  [`../ops/observability/hr-agent-alerts.yml`](../ops/observability/hr-agent-alerts.yml)
  and [`../ops/observability/README.md`](../ops/observability/README.md).
- The digest-pinned private monitoring topology and provisioned Apache-licensed
  operator dashboard are defined in
  [`../ops/observability/compose.reference.yml`](../ops/observability/compose.reference.yml).
  Deploy it only behind the authenticated operator proxy and with the mounted
  metric, notification, trace TLS/exporter, and Perses secret files described
  in the observability runbook.

For an incident, correlate sanitized request, trace, job, and provider IDs;
never paste candidate data, tokens, credentials, or provider payloads into the
incident record. Pause the affected idempotent queue or connector, preserve
evidence, fix the owning subsystem, replay through the supported control, and
require readiness plus the adjacent alert to remain healthy for a full alert
window.

## Cut over preserved legacy private files

Run this procedure with API and worker writers stopped for the target preview,
or with every supported writer deployed with the matching advisory-fence
contract. The migration itself also holds the shared fence; candidate merge and
reversal wait on its exclusive side. Never run against an unverified legacy
mount or an application runtime database role.

1. Record the exact source revision and Alembic head. Capture candidate,
   application, evaluation, interview, note, activity, and object counts plus a
   deterministic business-data projection hash.
2. Create an encrypted PostgreSQL custom-format dump and a mode-0600 archive of
   the explicit legacy storage directory. Verify the dump with
   `pg_restore --list`, the archive with `tar --list`, record SHA-256 and byte
   size for both, and retain them outside the working tree.
3. Record a deterministic source-tree inventory: relative key, size, and
   SHA-256. Confirm the primary private bucket and quarantine prefix have the
   expected pre-cutover state.
4. Inject the reviewed operator database URL, private-object buckets, malware
   scanner, and `LEGACY_PRIVATE_STORAGE_DIR` from the approved secret store.
   Mount the historical storage directory itself—for this repository layout,
   the bounded root is `.../apps/api/storage`. Historical database references
   such as `storage/cvs/<uuid>.pdf` are normalized by stripping exactly one
   leading `storage/` segment relative to that root. Do not point the setting
   at `.../apps/api`, `/`, or a parent containing unrelated files.
   Run the non-mutating pass from `apps/api`:

   ```bash
   uv run python -m app.scripts.migrate_legacy_private_objects \
     --batch-size 50
   ```

   Require exit zero, `status=dry_run_pass`, zero failures, all referenced
   resources validated, and a reviewed bounded orphan report. A tenant-filtered
   pass intentionally skips the global orphan scan, but duplicate ownership is
   still checked globally.
5. Recheck the database and source inventories against step 1, then apply:

   ```bash
   uv run python -m app.scripts.migrate_legacy_private_objects \
     --apply \
     --batch-size 50
   ```

   The command never removes the source. Require one ready registry object and
   one pending replacement deletion obligation per migrated resource, no
   quarantine residue, exact promoted size/SHA parity, unchanged business
   counts/projection, and an unchanged legacy source-tree inventory. Rerun both
   dry-run and apply; they must report no remaining unregistered resources and
   create no duplicate registry, usage, or outbox effects.
6. Start API/web without the deletion worker. Verify authorized download,
   sensitive-read audit, wrong-tenant/anonymous denial, a file-only candidate
   parse/score path, and the preserved RU/EN candidate and position views.
7. Take and verify a post-migration database-plus-object backup. Only then
   start the deletion worker. It must drain each digest-bound replacement row
   through the atomic local claim; a mismatch or tombstone requires operator
   review and must keep readiness degraded. Reconcile the canonical bucket and
   prove that only verified redundant legacy sources disappeared.

The 2026-08-24 preserved-preview procedure is complete. Its sanitized evidence
is `docs/evidence/preserved-data-legacy-cutover-e18-2026-08-24.json`: dry run
validated 62/62; apply migrated 62/62; the idempotent rerun scanned zero; 62
ready canonical objects reconciled with zero quarantine/unregistered entries;
authorized audited download passed; encrypted backup/restore met its RPO/RTO;
and the worker drained 62/62 digest-matched redundant sources with no retry,
DLQ, failure, or tombstone. The final global dry run found no unregistered
resource and reported seven opaque unreferenced files (two CV and five
assessment), which remain untouched for separate review. This closes the
internal cutover operation only; exact final-revision CI and external release
gates remain separate.

## Release and rollback

1. Freeze the schema/API contracts and generate the frontend client once.
2. Take and verify an encrypted database-plus-object backup before migration.
3. Apply expand migrations, deploy workers, deploy API replicas, then deploy
   the web BFF. Confirm the recorded Alembic head and readiness at each step.
4. Run RU/EN golden smoke, authorization/tenant canaries, one provider
   simulator job, and one private-object read/delete canary.
5. Contract migrations and old-column removal occur only in a later release
   after rollback compatibility and backup expiry are documented.

For the E18 candidate, require Alembic head `e18c4f6a0b32`, the recorded final
API result (737 collected: 727 passed, 10 deliberate skips, 0 failed in 840.12
seconds), and the strict real-stack Chromium/Firefox jobs locally. The pinned
Linux WebKit journey, exact image
build/scans, SBOM/signing, and remaining gates must run against the same
committed revision in CI; earlier candidate runs are regression baselines, not
final-release evidence. The API job has a 75-minute bounded timeout because a
healthy hosted-runner execution previously reached only 68% before the old
35-minute ceiling canceled it. The larger budget is not a pass: the complete
exact-revision job must still finish green.

For application rollback, restore the previous signed image set while the
expand schema remains in place. Do not downgrade a database that may contain
new-format writes. If data restoration is required, isolate traffic, follow
the recovery runbook into new database/object targets, verify hashes and forced
RLS, and switch deployment references only after security and operations
review. Pilot targets are RPO at most 24 hours and RTO at most four hours.

## Offboarding and privacy operations

Place legal holds before retention or erasure work. Candidate erasure must
finish or fail closed across database rows, private objects, derived AI/chat
and workflow data, communications, provider deletion obligations, caches, and
the 35-day backup-expiry manifest. A local deletion is not proof of remote
provider deletion. Record the provider acknowledgement or retain an
operator-visible unresolved obligation and do not report completion.

Migration `e18c4f6a0b32` serializes same-tenant writes against an active erasure
with PostgreSQL advisory locks. A caller receiving 409
`privacy_erasure_in_progress` and `Retry-After: 1` may retry after the current
erasure transaction completes; do not bypass the gate, edit affected rows
manually, or convert that response to `audit_unavailable`. Adjacent tenants
remain writable. The release schema check must report write-gate coverage for
112/112 tenant tables.

New AI attachments may be accepted only when the upload and retention inventory
contract covers every body and non-body surface in the format allowlist. This
includes PDF metadata, annotations, and form values and DOCX/XLSX properties,
relationships, comments, headers/footers, and other accepted OOXML text. Unsafe
or unsupported new content must return a sanitized validation error and leave
no registry object or blob. If unsupported legacy content makes retention
return 409, keep the candidate and object intact, preserve the audit evidence,
and use the supported replacement/redaction workflow; never force-delete the
candidate while its attachment cannot be inventoried.

Before deprovisioning a tenant, revoke sessions, API keys, public capabilities,
OAuth connections, webhooks, and model routing; drain/cancel its durable work;
export contractually required audit/portability data; execute approved erasure;
and retain only legally required pseudonymized accounting and expiry evidence.
