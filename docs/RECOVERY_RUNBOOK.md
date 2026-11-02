# PostgreSQL and private-object recovery runbook

## Recovery contract

`GET /enterprise/backup` is a policy-filtered **core-data portability export**.
Its JSON explicitly sets `restorable: false`; it does not contain the complete
application graph or object bytes and must never be presented as disaster
recovery.

Disaster recovery uses [`ops/recovery/hr_recovery.py`](../ops/recovery/hr_recovery.py).
One encrypted archive contains a PostgreSQL custom-format dump, the complete
private-object snapshot, a per-object SHA-256 inventory, required PostgreSQL
owner/security roles, timestamps, and a 35-day-or-shorter expiry. The external
receipt authenticates the entire ciphertext with an independent HMAC.

The restore command is fail-closed:

- it accepts database credentials and signing material only from environment
  variables;
- it refuses system/production-like names and requires a new database name
  containing `restore`, `recovery`, or `drill`;
- it requires a second exact confirmation for both database and object target;
- it never overwrites an existing database, local object directory, or S3
  prefix;
- S3 restore buckets must have versioning enabled; every restored object
  version carries a random per-run ownership marker, and rollback deletes only
  the exact immutable versions owned by that run;
- it verifies the receipt HMAC, encrypted-file digest, AES-GCM tag, dump hash,
  every object hash, Alembic marker, RLS/owner/grant contract, RPO, and RTO;
- on failure it removes only the newly created, validated recovery targets;
- `--cleanup-after-verify` also proves rollback after a successful drill.

Pilot targets are RPO no greater than 24 hours and RTO no greater than 4 hours.
Encrypted backup copies expire within 35 days.

## Prerequisites and key custody

The recovery runner needs Python 3.12 with the API lock installed, PostgreSQL
16 client binaries, and `aws` CLI v2 for S3-compatible storage. For local
Docker development, `--pg-container <container>` runs the PostgreSQL clients in
that container without putting the password in command arguments.

Generate the recovery key pair on an encrypted operator workstation:

```bash
install -d -m 700 /secure/operator/hr-agent-recovery
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 \
  -out /secure/operator/hr-agent-recovery/private.pem
chmod 600 /secure/operator/hr-agent-recovery/private.pem
openssl pkey -in /secure/operator/hr-agent-recovery/private.pem -pubout \
  -out /secure/operator/hr-agent-recovery/public.pem
openssl rand -base64 48
```

Store the final command's value in the secret manager as
`HR_RECOVERY_MANIFEST_HMAC_KEY`; do not paste it into a shell command, config
file, CI log, or ticket. It must decode to at least 32 bytes and be independent
from JWT, MFA, database, object-store, and integration secrets. Backup workers
receive only the RSA public key and HMAC secret. Restore operators receive the
private key just in time; if it has a passphrase, inject that as
`HR_RECOVERY_PRIVATE_KEY_PASSPHRASE`.

Back up the HMAC secret and RSA private key in a separately controlled escrow.
Losing either makes the archive intentionally unrestorable. Rotate by keeping
the previous key material until every archive it protected has expired.

## Scheduled backup

1. Start an identified maintenance window. Block writes at the ingress, drain
   the PostgreSQL durable queues, stop workers, and wait for active mutations
   to finish. The CLI records `--maintenance-window` as an operator
   attestation; it cannot place external load balancers in read-only mode.
2. Verify the database/object store are healthy and the previous successful
   backup is less than 24 hours old.
3. Inject `HR_RECOVERY_DATABASE_URL` and
   `HR_RECOVERY_MANIFEST_HMAC_KEY` from the secret manager. Never supply a raw
   database URL on the CLI.
4. Run a dry plan, then the backup.

Local/reference object storage:

```bash
cd apps/api
uv run python ../../ops/recovery/hr_recovery.py backup \
  --object-source local \
  --local-object-root /srv/hr-agent/private-objects \
  --output-dir /srv/hr-agent/encrypted-backups \
  --public-key /run/secrets/hr-recovery-public.pem \
  --maintenance-window change-2026-08-22-001 \
  --retention-days 35 \
  --dry-run

uv run python ../../ops/recovery/hr_recovery.py backup \
  --object-source local \
  --local-object-root /srv/hr-agent/private-objects \
  --output-dir /srv/hr-agent/encrypted-backups \
  --public-key /run/secrets/hr-recovery-public.pem \
  --maintenance-window change-2026-08-22-001 \
  --retention-days 35
```

S3-compatible live object storage uses ambient `AWS_ACCESS_KEY_ID`,
`AWS_SECRET_ACCESS_KEY`, and optional session credentials, plus:

```text
HR_RECOVERY_S3_BUCKET=hr-agent-private
HR_RECOVERY_S3_PREFIX=private/
HR_RECOVERY_S3_ENDPOINT=https://minio.internal.example
```

Run the same backup command with `--object-source s3` and omit
`--local-object-root`. Resume API/worker traffic only after the command returns
success and both `.hrb` and `.receipt.json` have been copied to a separate,
encrypted, versioned backup bucket with object lock. Apply
[`ops/recovery/s3-backup-lifecycle.json`](../ops/recovery/s3-backup-lifecycle.json)
to that backup prefix; the live private-object bucket must use its own policy.

The working directory temporarily holds the database dump and object snapshot
with mode `0600` inside a random `0700` directory, then removes them. Place the
working/output filesystem on encrypted storage and alert on abnormal
termination so abandoned scratch data is removed.

## Archive verification and retention

Authenticate, decrypt, and checksum an archive without creating recovery
targets:

```bash
cd apps/api
uv run python ../../ops/recovery/hr_recovery.py verify \
  --archive /srv/hr-agent/encrypted-backups/ARCHIVE.hrb \
  --receipt /srv/hr-agent/encrypted-backups/ARCHIVE.receipt.json \
  --private-key /run/secrets/hr-recovery-private.pem
```

Run `verify` after every copy and at least weekly against one retained archive.
List expired local backups first; deletion requires a separate apply flag and
the exact current UTC date:

```bash
uv run python ../../ops/recovery/hr_recovery.py prune-local \
  --directory /srv/hr-agent/encrypted-backups

uv run python ../../ops/recovery/hr_recovery.py prune-local \
  --directory /srv/hr-agent/encrypted-backups \
  --apply \
  --confirm-delete-before YYYY-MM-DD
```

S3 expiration is enforced by lifecycle policy and monitored by inventory.
Retention jobs must retain signed deletion/inventory evidence without retaining
candidate content.

## Isolated restore drill

On a new PostgreSQL recovery cluster, first create the three non-login security
roles. This is cluster-level state and is intentionally not hidden inside a
database dump:

```bash
psql -X -v ON_ERROR_STOP=1 -d postgres \
  -f ops/recovery/bootstrap_roles.sql
```

Any additional object-owner role named in the backup manifest must also exist
on the target. Login roles and passwords come from infrastructure secrets;
never recover credential hashes from a database archive.

Inject a target-cluster admin connection as `HR_RECOVERY_DATABASE_URL`, the
HMAC key, and the private-key passphrase if applicable. The URL's database is
only the administrative connection database (normally `postgres`); the tool
creates the isolated target named by `--target-database`.

Local object target drill:

```bash
cd apps/api
uv run python ../../ops/recovery/hr_recovery.py restore \
  --archive /srv/hr-agent/encrypted-backups/ARCHIVE.hrb \
  --receipt /srv/hr-agent/encrypted-backups/ARCHIVE.receipt.json \
  --private-key /run/secrets/hr-recovery-private.pem \
  --target-database pilot_restore_20260822 \
  --confirm-restore-to pilot_restore_20260822 \
  --target-object-kind local \
  --restore-local-object-root /srv/hr-agent/pilot-object-restore-20260822 \
  --confirm-object-target /srv/hr-agent/pilot-object-restore-20260822 \
  --evidence /srv/hr-agent/recovery-evidence/2026-08-22.json \
  --max-rpo-seconds 86400 \
  --max-rto-seconds 14400 \
  --cleanup-after-verify
```

For S3, use a dedicated recovery bucket with versioning enabled, then configure
a new, empty prefix whose name contains `restore`, `recovery`, or `drill`:

```text
HR_RECOVERY_RESTORE_S3_BUCKET=hr-agent-private-recovery
HR_RECOVERY_RESTORE_S3_PREFIX=drill/2026-08-22/
HR_RECOVERY_S3_ENDPOINT=https://minio.internal.example
```

Use `--target-object-kind s3`, omit `--restore-local-object-root`, and pass the
exact `s3://bucket/prefix` to `--confirm-object-target`. Duplicate/non-empty
targets fail before mutation. Versioning is mandatory because a stale prefix
inspection cannot prove exclusive ownership: failed restores and drill cleanup
discover the random ownership metadata and delete only those exact version
IDs, preserving any concurrent writer's versions.

Do not use `--cleanup-after-verify` for a real recovery. After independent
application/API checks and security approval, switch the deployment's database
secret and private-object prefix to the verified targets. Keep the old targets
read-only until the change window closes. Roll back by restoring the old
deployment references; the tool never modifies them.

## Tenant-scoped portability recovery

[`ops/recovery/tenant_recovery.py`](../ops/recovery/tenant_recovery.py) is the
operator path for restoring one contracted tenant without overwriting other
organizations already present in the target. It preserves the source
organization UUID; it never attempts generic UUID rewriting. The target may
contain other tenants, but the selected UUID must be absent from
`organizations` and every tenant table, and source and target must have the
same migration head and tenant-table contract.

The source URL must use a dedicated LOGIN role with only the reviewed SELECT
grants. The tool rejects superuser, `BYPASSRLS`, owner membership, inherited
owner rights, missing FORCE RLS/policies, and unsafe grants. Database inventory,
relationship checks, object references, and `pg_dump` share one exported
repeatable-read snapshot. The rendered column-insert dump is then inspected
row by row to prove every archived row belongs to the selected organization
and to the declared table/count inventory.

Back up locally stored tenant objects with exact organization, operator, ticket,
and maintenance-window confirmations:

```bash
cd apps/api
uv run python ../../ops/recovery/tenant_recovery.py backup \
  --organization-id ORGANIZATION_UUID \
  --confirm-organization-id ORGANIZATION_UUID \
  --operator-id operator@example.test \
  --authorization-ticket CHG-REVIEWED-ID \
  --maintenance-window CHANGE-WINDOW-ID \
  --object-source local \
  --local-object-root /srv/hr-agent/private-objects \
  --output-dir /srv/hr-agent/encrypted-tenant-backups \
  --public-key /run/secrets/hr-recovery-public.pem \
  --retention-days 35 \
  --dry-run
```

Inject `HR_TENANT_BACKUP_DATABASE_URL` and
`HR_RECOVERY_MANIFEST_HMAC_KEY` from the secret manager. Repeat without
`--dry-run` only after reviewing the plan. For S3, use `--object-source s3` and
inject `HR_TENANT_BACKUP_S3_BUCKET` plus the same bounded endpoint/ambient AWS
credentials used by full recovery. Pre-registry absolute legacy paths are
accepted only for local storage and must resolve inside the tenant/category
namespace; ambiguous or cross-tenant paths fail closed.

Authenticate and inspect the `.hrt` archive before restore:

```bash
uv run python ../../ops/recovery/tenant_recovery.py verify \
  --archive /srv/hr-agent/encrypted-tenant-backups/ARCHIVE.hrt \
  --receipt /srv/hr-agent/encrypted-tenant-backups/ARCHIVE.receipt.json \
  --private-key /run/secrets/hr-recovery-private.pem
```

Restore to a reviewed target using `HR_TENANT_RESTORE_DATABASE_URL`:

```bash
uv run python ../../ops/recovery/tenant_recovery.py restore \
  --archive /srv/hr-agent/encrypted-tenant-backups/ARCHIVE.hrt \
  --receipt /srv/hr-agent/encrypted-tenant-backups/ARCHIVE.receipt.json \
  --private-key /run/secrets/hr-recovery-private.pem \
  --organization-id ORGANIZATION_UUID \
  --confirm-organization-id ORGANIZATION_UUID \
  --confirm-target-database REVIEWED_TARGET_DATABASE \
  --operator-id operator@example.test \
  --authorization-ticket CHG-REVIEWED-ID \
  --target-object-kind local \
  --restore-local-object-root /srv/hr-agent/private-objects \
  --confirm-object-target /srv/hr-agent/private-objects \
  --evidence /srv/hr-agent/recovery-evidence/TENANT-VERSION.json \
  --dry-run
```

Repeat without `--dry-run` only after review. Restore holds a live
database/organization advisory lock and an operation ownership sentinel through
the database and object mutation. Local rollback checks inode/digest ownership;
S3 rollback deletes only versions tagged with that restore's random operation
identity. Authenticated evidence is published atomically without overwrite,
and success writes an operator audit event to the restored tenant. Any rollback
ownership loss or residue fails visibly; never report partial cleanup as a
successful restore.

The current release-candidate recovery harness records 36 passing tests and
three opt-in skips in its default run. The separately enabled disposable PostgreSQL
tenant-backup/restore/rollback drill passes 1/1 and proves excluded-tenant,
snapshot, concurrent-lock, audit, object-hash, and rollback canaries. Production
S3 and customer-data drills still require reviewed infrastructure and custody.

## 10,000-candidate performance release gate

Use an isolated PostgreSQL database ending in `_perf_test`. The benchmark is
skipped by the normal suite and therefore cannot add noise to ordinary CI. It
loads exactly 10,000 candidates, warms caches, measures 30 authenticated
indexed search requests and 30 authenticated local mutations, computes
nearest-rank p95, asserts 500 ms/1 s targets, and writes JSON evidence.

```bash
cd apps/api
RUN_PERFORMANCE_TESTS=1 \
TEST_DATABASE_URL=postgresql+asyncpg://USER@HOST:5432/hr_candidate_perf_test \
PERFORMANCE_EVIDENCE_PATH=/absolute/evidence/candidate-10k.json \
uv run pytest -q -s tests/performance/test_candidate_10k.py
```

Inject the database password without placing it in the command or URL shown in
logs. Run the gate on production-equivalent CPU/storage before each pilot
release; local evidence is a regression baseline, not capacity certification.

## Drill evidence and cadence

- Backup cadence: at least daily; alert before age reaches 24 hours.
- Archive checksum verification: every backup and weekly retained-sample drill.
- Isolated restore/rollback drill: before pilot launch and monthly thereafter.
- Tenant-scoped backup/restore/rollback drill: before using tenant portability
  and after any tenant-table, RLS, object-namespace, or recovery-tool change.
- S3/MinIO restore drill: before enabling that backend and after storage or key
  rotation.
- Evidence records: archive ID, migration head, object count/bytes, checksum
  result, RLS/role/grant result, RPO, RTO, rollback result, reviewer, and ticket.

The checked-in local evidence under [`docs/evidence`](evidence) proves the
workflow and performance harness ran in the reference environment. A release
reviewer must replace it with evidence from the final migration head and
production-equivalent S3/MinIO infrastructure before signing pilot acceptance.
