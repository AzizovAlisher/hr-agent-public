# Pilot integration runtime

## Contract

All pilot connectors use `IntegrationConnection`. A connection is always in one
of these server-controlled states:

`unavailable`, `disconnected`, `connecting`, `connected`, `degraded`, `expired`,
or `revoked`.

Only `connected` connections accept new external-effect jobs. `degraded`
connections may finish already-queued retryable work but must pass health before
new work is created. Revocation deletes local encrypted credentials immediately,
even when the provider cannot be reached.

Provider secrets, OAuth PKCE verifiers, opaque cursors, job payloads, inbound
webhook bodies, and outbound webhook bodies are AES-256-GCM encrypted. AAD binds
each value to its organization, connection, record, and purpose. The wrapping
key is a dedicated operator secret and may not be derived from JWT, MFA, database,
or provider credentials. API responses expose only configured credential key
names and connection health; secret values are write-only.

## Pilot provider capabilities

| Provider | Real operations | Certification boundary |
|---|---|---|
| Google Workspace | OAuth PKCE, refresh/revoke, Gmail send, Calendar create/update/delete and health | Live certification needs a customer test Workspace and approved OAuth client |
| Microsoft 365 | OAuth PKCE/refresh, Graph sendMail, Calendar busy-window/create/update/delete and health; disconnect deletes this product's encrypted tokens locally | Live certification needs a customer test tenant and approved app registration |
| Telegram | Bot `getMe`, `sendMessage`, and vacancy broadcast | Live certification needs a bot token and target test channel; inbound bot updates are not advertised in the pilot capability set |
| hh.ru | OAuth PKCE/refresh/revoke, `/me`, vacancy publish, employer negotiation collection intake | Live certification needs an approved dev.hh.ru application and employer test account |
| Somon.tj | Tracked application links and validated mapped CSV/XLSX imports | No scraping is implemented; deeper publication/intake requires an official partnership/API |
| CSV/XLSX | 10 MiB upload cap, 50 MiB expanded XLSX cap, mapping preview, masked contact preview, 500-row synchronous import, replay-safe runs | Fully local |
| Generic webhook | Versioned HMAC-SHA256 inbound/outbound envelopes, timestamp/replay validation, event-ID/payload-fingerprint conflict detection, durable delivery inspection and DLQ | Target host must be operator allowlisted |
| Generic assessment | Invitation, attempt status, result retrieval, evidence normalization, signed result callbacks | Vendor base host must be operator allowlisted and implement the `/v1` contract |

The hh.ru adapter follows the official API at <https://github.com/hhru/api>.
Telegram follows <https://core.telegram.org/bots/api>. Google uses the official
Gmail and Calendar REST endpoints; Microsoft uses Graph v1.0. Tests intercept
these exact endpoints with local `httpx.MockTransport` simulators and never need
live credentials.

Microsoft does not expose an app-scoped OAuth token-revocation endpoint. The
Graph `revokeSignInSessions` action would invalidate the person's sessions and
refresh tokens across unrelated applications, so the connector deliberately
does not call it or request `User.RevokeSessions.All`. Disconnect remains
immediate and fail-safe by deleting this product's encrypted access and refresh
tokens and marking the connection revoked.

## Durable external effects

`IntegrationJob` stores a unique organization/connection/operation/idempotency
tuple and a fingerprint covering both references and the encrypted payload.
Reusing a key with the same request returns the original job. Reusing it with a
different request fails with `idempotency_conflict`.

Workers claim jobs using `FOR UPDATE SKIP LOCKED`, bounded leases, at most ten
attempts, exponential retry, terminal `dead_letter`, cancellation, and explicit
admin replay. Provider POSTs that do not guarantee idempotency are attempted once
per claim; an ambiguous network result is surfaced instead of silently duplicating
an email, Telegram post, or vacancy. Microsoft event creates include Graph's
`transactionId`; Google conference creation includes a stable `requestId`.

Opaque provider page tokens and sync checkpoints use a named, encrypted cursor
service. Cursor values are bounded, tenant/connection/purpose bound with AAD,
atomically replaced, and never returned by public API responses. Successful
provider responses are streamed through a 2 MiB bound before JSON parsing.

Outbound webhooks are the exception: contract consumers must deduplicate the
stable `X-HR-Webhook-Id`, so retrying the exact signed event is safe.

## Forced-RLS worker topology

Production discovery never scans tenant tables through the runtime or worker
role. A `NOLOGIN BYPASSRLS` broker owns one fixed `SECURITY DEFINER` claim
function for each queue: provider jobs, outbound webhook deliveries,
communications, interview reminders, workflow runs, AI mission steps, private
object deletions, and per-organization maintenance. A claim returns only
`work_id`, `organization_id`, a rotating lease token, and its expiry.

Each claimed item is processed in a fresh tenant-bound transaction. Payload
reads, domain mutations, retry/DLQ transitions, and deletion of the exact
generic lease commit together. An expired or rotated token cannot acknowledge
work and rolls back the stale worker's database changes. The direct-query path
exists only when `TENANT_RLS_MODE=off` is explicitly selected for local tests
or development; production configuration requires `forced`.

The application and worker connections must use `NOLOGIN`-owner-independent
login roles that are neither superusers nor `BYPASSRLS`, and neither role may
own a protected tenant table. Schema migrations run under a separate operator
role. The broker is `NOLOGIN`; it owns only the reviewed fixed functions needed
to cross the discovery boundary. Production readiness queries PostgreSQL's
catalogs and fails when the connected role can bypass RLS, owns a protected
table, or when any `public` table containing `organization_id` (plus
`organizations`) lacks both ENABLE and FORCE RLS. Health output exposes only
booleans and counts, never database role names or connection strings.

Candidate activity inserts materialize event workflows transactionally.
Per-tenant maintenance claims materialize clock-derived inactivity and overdue
feedback inputs without executing them inline. The workflow worker then runs
the same queued contract as every other durable consumer.

Private-object erasure uses its own outbox and claim function. The tenant
processor verifies the native claim token, immutable key/SHA snapshot, legal
hold, and tenant key prefix before an idempotent object-store DELETE. The
registry row and outbox completion settle with the generic lease; storage
failures use bounded exponential retry and attempt ten is visible as DLQ.

## Signed webhook envelope

```json
{
  "version": "2026-08-01",
  "id": "evt_opaque-stable-id",
  "type": "application.stage.changed",
  "created_at": "2026-08-22T10:00:00Z",
  "data": {}
}
```

Headers:

- `X-HR-Webhook-Version: 2026-08-01`
- `X-HR-Webhook-Id: <envelope.id>`
- `X-HR-Webhook-Timestamp: <unix seconds>`
- `X-HR-Webhook-Signature: v1=<hex HMAC-SHA256>`

The signed bytes are `<timestamp>.<exact request body>`. The receiver accepts a
five-minute signature window, rejects invalid or stale signatures without
enumerating the connection, and enforces uniqueness of each event ID within its
owning connection or account. Replaying the same event is acknowledged without a second mutation;
reusing an event ID with different bytes fails.

Public communication-sandbox and generic-source receivers additionally require
the one-time-issued/rotatable raw secret in `X-Webhook-Secret`; only its digest
is stored. Their exact request stream is capped at 256 KiB before resource
lookup or JSON parsing. Communication events use
`communication.message.received`; source events use
`source.candidate.created`. Both validate envelope version/type/time before
domain mutation, persist the event identity and SHA-256 payload fingerprint,
acknowledge an identical replay, and return `webhook_event_conflict` when the
same event ID arrives with different bytes. Missing resources and invalid
credentials/signatures share a non-enumerating response.

## Operator configuration

- `INTEGRATION_CREDENTIAL_ENCRYPTION_KEY`: URL-safe base64 encoding of exactly 32 random bytes; required in production.
- `INTEGRATION_CREDENTIAL_KEY_ID`: operator key identifier used for rotation tracking.
- `INTEGRATION_OAUTH_REDIRECT_ORIGINS`: comma-separated exact origins; paths, wildcards, userinfo, queries, and fragments are rejected.
- `INTEGRATION_OUTBOUND_HOST_ALLOWLIST`: comma-separated exact public DNS hosts for generic assessment and outbound webhook targets.
- `INTEGRATION_HTTP_TIMEOUT_SECONDS`: provider request timeout, 2–60 seconds.
- `HH_USER_AGENT`: production operator identity with an email address or HTTPS contact.
- `INTEGRATION_WORKER_ENABLED`, `INTEGRATION_WORKER_POLL_SECONDS`, and `INTEGRATION_WORKER_CLAIM_LIMIT`: separate-worker controls.
- `TENANT_RLS_MODE`: `forced` in production; `off` is the explicit local/test compatibility mode.

Communication and source webhook secrets are returned only at account/integration
creation or explicit rotation. Rotate after suspected disclosure, update the
sender atomically, and prove the previous secret is rejected. Do not record a
raw secret, signed body containing candidate data, or provider payload in logs
or incident evidence.

Outbound dynamic targets require HTTPS, port 443, an exact allowlist match, and
public DNS answers. Redirect following is disabled. The target is resolved and
checked again immediately before every delivery to block private, loopback,
link-local, metadata, and DNS-rebinding targets.

## Verification

Focused contract suite:

```bash
TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/hr_agent_integrations_test \
  uv run pytest -q \
  tests/test_integration_runtime.py \
  tests/test_integration_provider_contracts.py \
  tests/test_regional_sourcing.py \
  tests/test_communications.py \
  tests/test_scheduling.py
```

The suite covers tenant isolation, encrypted-at-rest canaries, OAuth PKCE and
state replay, refresh tokens, endpoint and SSRF policy, safe versus unsafe HTTP
retry, Google, Microsoft, Telegram, hh.ru and assessment request contracts,
webhook signature/timestamp/replay/conflict handling, durable delivery, job
idempotency/leases and poison-job isolation, encrypted cursor replacement,
provider response-size bounds, Somon tracked links, mapped file preview, and
replay-safe imports.

Worker and readiness regression coverage additionally lives in
`tests/test_worker_claims.py`, `tests/test_private_object_worker.py`,
`tests/test_operations.py`, and `tests/test_workflows.py`. It covers two-worker
claim exclusion, crash expiry/reclaim, release, rotated-token stale
acknowledgement, cross-tenant processing sessions, bounded retry/DLQ,
idempotent private deletion, exact Alembic-head checks, object-store
write/read/delete probes, queue age, and worker-heartbeat coverage.
