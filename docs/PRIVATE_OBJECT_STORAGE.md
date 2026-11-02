# Private object storage runbook

Candidate CVs, assessment answers, technical-task files, owned interview
recordings, and short-lived AI session attachments are opaque objects. They are
never static files and are never addressed by a caller-provided path. Recording
media uses `private/<org>/recordings/`; AI attachments use
`private/<org>/ai/` with a 24-hour registry retention deadline. The only
anonymous namespace is organization branding under `logos/`.

## Runtime contract

- Production requires `BLOB_STORE_BACKEND=s3`, HTTPS `S3_ENDPOINT`, distinct
  private `S3_BUCKET` and branding-only `S3_PUBLIC_BUCKET` values, an HTTPS
  `S3_PUBLIC_BASE_URL`, and `MALWARE_SCANNER_BACKEND=clamav`.
- `local` and the deterministic signature scanner are development/test
  backends only. Local object and spool files use owner-only permissions.
- Inbound bodies are streamed into a 0600 spool with an explicit ceiling.
  Candidate, assessment, task, and recording files are capped at 15 MiB; logos
  are capped at 2 MiB. Reading stops as soon as the next byte exceeds the
  limit.
- Extension, declared MIME, magic bytes, parser structure, page/pixel/archive
  limits, polyglot markers, unsafe XML/relationships, macros, embedded objects,
  SVG/HTML, and archive expansion are checked before promotion.
- A validated object is uploaded to `quarantine/<org>/<category>/<uuid>.upload`,
  scanned, optionally parsed, then copied/renamed to
  `private/<org>/<category>/<uuid>.<ext>`. Every failure removes spool,
  quarantine, and any partially promoted destination.
- Branding promotions use versioned `logos/<org>/<uuid>.<ext>` keys so a
  failed database update cannot overwrite or erase the previously committed
  logo. Local development serves only that directory. S3 deployments keep the
  existing `/storage/logos/...` client contract through a strict key validator
  and redirect to `S3_PUBLIC_BASE_URL`; no private or quarantine key can pass
  through that mount.
- Private reads query a tenant-owned record, validate the opaque key's tenant
  and category, enforce field policy, add a sensitive-read audit event, and
  stream through the API with `private, no-store`, `nosniff`, and a sanitized
  attachment disposition. Private S3 URLs are never returned to clients.

## Legacy private-object cutover

Pre-registry candidate, assessment, task, and recording files are migrated by
`python -m app.scripts.migrate_legacy_private_objects`. The command is a dry
run unless `--apply` is supplied. It requires an explicit
`LEGACY_PRIVATE_STORAGE_DIR`, a PostgreSQL operator role that can inspect forced
RLS globally, the production malware scanner, and the configured primary
`BlobStore`. It never deletes a legacy source.
`LEGACY_PRIVATE_STORAGE_DIR` names the historical storage directory itself
(`.../apps/api/storage` in the reference layout). The compatibility normalizer
strips exactly one leading `storage/` from old references such as
`storage/cvs/<uuid>.pdf`; configuring the API package parent or another broader
directory is outside the reviewed contract.

The cutover reads resources and files in bounded pages. Its ownership preflight
is global even when `--organization-id` limits the applying pass, because the
oldest `cvs/<uuid>.<ext>` keys are not tenant-namespaced. A shared PostgreSQL
advisory fence covers preflight through the last item; candidate merge and
reversal take the matching exclusive transaction fence before they can change
legacy references. Duplicate, cross-tenant, symlinked, missing, changed, or
unsupported references fail closed.

For each applying item, the service:

1. locks and rechecks the resource and exact normalized legacy key;
2. validates the document with the current bounded parser, obtains a fresh
   malware verdict, and proves size/SHA-256 stability;
3. promotes a new opaque tenant-private object;
4. atomically commits the `private_objects` row, resource
   `private_object_id`/compatibility path, usage delta, and a digest-bound
   `legacy_private_object_deletions` row; and
5. removes an unregistered promoted object if the database unit fails.

The deletion worker, not the migration command, removes the redundant local
copy. It atomically renames the exact source to a deterministic same-directory
tombstone, opens it through directory file descriptors with no symlink
following, rechecks inode, size, timestamps, and SHA-256, and only then unlinks
the claimed name. A concurrent replacement is never overwritten or deleted;
the verified bytes are restored without replacement or retained under the
tombstone for operator inspection. Crash tombstones resume idempotently.

Before applying to preserved pilot data, retain and verify a database dump and
legacy-file archive. Record pre/post row counts, business-data projection
hashes, source-tree hash/bytes, registry/object counts, per-object size/digest
matches, orphan digests, and pending deletion obligations. The protected
2026-08-24 operation completed this protocol: 62/62 sources validated and
migrated, 62 ready objects reconciled, authorized audited download and
post-cutover backup/restore passed, and the worker drained 62/62 verified
redundant copies without retry, dead letter, failure, or tombstone. The stable
business projection hash did not change. Seven opaque unreferenced objects
(two CV and five assessment) remain isolated and untouched. Sanitized counts,
hashes, and recovery metrics are recorded in
`docs/evidence/preserved-data-legacy-cutover-e18-2026-08-24.json`; exact final-
revision CI and external acceptance are separate release gates.

## MinIO reference configuration

Use two dedicated buckets and a service account with explicit access to each.
The private bucket grants the API identity object read/write/delete only for
`private/` and `quarantine/`; it has no anonymous policy. The public bucket
grants the API identity object read/write/delete only for `logos/` and its
non-public quarantine prefix. The identity has no administrative or
bucket-policy permissions.
Enable bucket versioning, server-side encryption, access logs, and TLS with a
trusted certificate. Do not put credentials in endpoint URLs.

Only branding may be anonymous. A minimal statement on the public bucket is
scoped to `arn:aws:s3:::hr-agent-public/logos/*`; the private bucket has no
anonymous statement:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {"AWS": ["*"]},
      "Action": ["s3:GetObject"],
      "Resource": ["arn:aws:s3:::hr-agent-public/logos/*"]
    }
  ]
}
```

Point `S3_PUBLIC_BASE_URL` at the bucket/CDN root that contains `logos/`.
Browser CORS, if needed, allows only `GET` and `HEAD` for the trusted web
origins. Browser writes always go to the API; private prefixes require no
browser CORS. Add a lifecycle rule that removes `quarantine/` objects after 24
hours and expired noncurrent versions according to the approved retention
period. Lifecycle is recovery hygiene, not the primary deletion mechanism.

The checked-in `docker-compose.dev.yml` bootstraps this exact two-bucket policy
with pinned MinIO images, versioning, a one-day quarantine lifecycle, and a
least-privilege application identity. Its object and Valkey connections are
plain HTTP/TCP for loopback development only; production validation requires
TLS endpoints and external secret injection.

Example local MinIO values:

```dotenv
BLOB_STORE_BACKEND=s3
S3_ENDPOINT=https://minio.internal.example
S3_BUCKET=hr-agent-private
S3_PUBLIC_BUCKET=hr-agent-public
S3_ACCESS_KEY=hr-agent-runtime
S3_SECRET_KEY=<independent secret>
S3_REGION=us-east-1
S3_PUBLIC_BASE_URL=https://assets.example.com
MALWARE_SCANNER_BACKEND=clamav
CLAMAV_HOST=clamav.internal
CLAMAV_PORT=3310
```

The reference deployment pins official ClamAV Alpine LTS 1.4.6 by immutable
digest. ClamAV must receive signature updates and fails readiness when it is
unavailable, returns malformed version metadata, reports a future-invalid
timestamp, or is older than the fixed 48-hour threshold. The alert rule uses
the same threshold. The application fails each upload closed on connection,
protocol, or timeout errors; it never falls back to the local signature scanner
in production. The live compatibility gate proves version/freshness, a clean
scan, and harmless EICAR detection.

## Operations and recovery

- Alert on scanner-unavailable, promotion, deletion, and reconciliation
  failures. Logs may contain request/object record IDs but not raw object keys,
  filenames, file contents, provider response bodies, or credentials.
- Back up the database registry and versioned object bucket as one recovery
  set. Restore into an isolated account, reconcile hashes and tenant ownership,
  then enable reads.
- A durable deletion outbox owns retries. Database state must not claim final
  erasure until the provider delete succeeds. A periodic reconciler detects
  ready objects without owners, missing ready objects, stale quarantine, and
  provider objects absent from the registry.
- Assessment replacement/revocation, task and recording replacement/deletion,
  and candidate file replacement enqueue deletion. Legal holds on recording
  media protect the owning candidate retention closure until explicitly
  released. Enterprise retention uses the same outbox for every registered
  primary and derived object. Verified pre-registry local paths use a separate
  digest-bound outbox and worker; no resource mutation or privacy erasure
  commits unless its legacy deletion row commits in the same transaction.
- Candidate erasure first revalidates every outstanding cutover-source size and
  digest. It adopts and accelerates pending replacement rows, resets an
  exhausted replacement retry budget, and fails the entire erasure closed if
  the source has changed. All matching registry rows, including already
  `deleted` tombstones, have their subject-facing resource ID and descriptive
  metadata scrubbed; completed storage evidence remains pseudonymous.
- `LEGACY_PRIVATE_STORAGE_DIR` is the explicit, owner-controlled compatibility
  mount for pre-registry files in production. The worker validates a closed
  category/root/UUID/extension contract and recorded size/SHA-256 before
  deletion. A missing mount or changed file retries and eventually surfaces in
  queue health; it never broadens a path or deletes a mismatch.
- Reconciliation persists separate tenant cursors for the registry,
  `private/<org>/`, and `quarantine/<org>/`. Durable progress uses the last
  canonical key and S3 `StartAfter`, so bounded scans wrap and eventually visit
  newly inserted keys that sort before the current cursor. Dry runs never
  advance cursors.
- Rotating the S3 credential is independent of JWT, MFA, integration, and
  database secrets. Revoke the previous credential only after every API and
  worker replica has adopted the replacement.

Set the reverse proxy or load-balancer request-body ceiling to the same value
as `REQUEST_BODY_MAX_BYTES`. The ASGI middleware stops multipart parsing and is
a required application backstop, but it cannot prevent an upstream proxy from
buffering a request before forwarding it. Alert whenever reconciliation reports
`listing_truncated`; it now means more work remains after the durable cursor,
rather than that later keys can starve.

PDF/Office/image validation and text extraction execute in bounded child
processes with wall-clock timeouts and resource limits. Sourcing spreadsheets
and raw-source imports use the same bounded-process pattern; timeout, crash, or
malformed parser output fails closed and leaves no promoted or orphan object.

PDF action detection is semantic rather than a byte-marker search. The
validator verifies complete revision/EOF and cross-reference boundaries, then
walks the reachable parsed object graph without interpreting compressed font or
image stream bytes as actions. Explicit local destinations and user-initiated
HTTP(S), mail, or telephone links are allowed. JavaScript, launch/mutating or
unknown actions, automatic URI actions, unsafe schemes, embedded/file
attachments, rich media, XFA, active annotations, malformed revision chains,
and bytes appended after the final logical EOF remain rejected. This preserves
safe historical CVs whose opaque font streams happen to contain action-like
tokens while keeping executable and polyglot surfaces fail closed.
