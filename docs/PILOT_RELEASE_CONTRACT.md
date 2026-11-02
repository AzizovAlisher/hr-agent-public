# Private B2B Pilot Release Contract — E18 Candidate

This document is the repository source of truth for pilot scope and release
decisions. A feature is pilot-ready only when its API authorization, tenant
isolation, UI, RU/EN copy, telemetry, failure behavior, and automated tests are
complete. Hidden or disabled controls do not count as implementation.

The current candidate is **NOT GO**. Its repository migration head is
`e18c4f6a0b32`; this document distinguishes locally verified evidence from
exact-revision CI, credentialed provider certification, credential-incident
closure, and human approval.

## Launch boundary

- Manual organization provisioning and contract entitlements; no public signup
  or self-service billing.
- Russian and English core journeys.
- Mandatory connectors: Google Workspace, Microsoft 365, Telegram, hh.ru,
  Somon.tj tracked link/import, CSV/XLSX, and signed REST/webhooks.
- Somon.tj is never scraped. Coding execution, psychometric libraries, video
  interviewing, WhatsApp, and production SSO remain capability-gated.
- Managed OpenRouter requires tenant opt-in. Private vLLM endpoints require an
  operator allowlist.
- Default candidate retention is 365 days, contract-configurable from 30 to 730
  days. Approved live erasure is immediate; encrypted backups expire within 35
  days unless a documented legal hold applies.

## Architectural invariants

1. FastAPI is the authoritative policy boundary. UI visibility is never an
   authorization control.
2. Every tenant-owned query and relation is constrained to the current
   organization. Cross-tenant identifiers do not reveal whether data exists.
3. Candidate-to-position workflow state lives on `Application`; legacy `Score`
   fields are compatibility data only.
4. Private files are never statically mounted. Downloads require tenant and
   field authorization, safe content disposition, `no-store`, and audit.
5. Long-running or externally visible effects use durable, idempotent jobs and
   an outbox. Valkey may accelerate limits or cache data but is not authoritative.
6. All AI features use the tenant-aware gateway, enforce residency and budgets,
   record provenance, and require a human for irreversible hiring decisions.
7. Connector credentials are encrypted and connectors expose a common lifecycle,
   capability, health, cursor, job, webhook, and error contract. Communication
   and sourcing callbacks use raw-body HMAC signatures, bounded bodies,
   timestamp/replay checks, versioned envelopes, and persisted event
   ID/payload fingerprints.
8. Public capability tokens are random, stored as digests, expiring, revocable,
   replay-aware, and returned only when created or rotated.
9. Collections use stable cursor pagination. Public submissions and external
   mutations support idempotency keys.
10. Errors use `application/problem+json` with a stable `code`, HTTP `status`,
    `request_id`, and optional field errors.
11. Public readiness returns only `status` and `checked_at` with `no-store`.
    Detailed dependency state is available only to authenticated identities
    with `enterprise.manage`.
12. Candidate erasure holds an exclusive tenant gate from inventory through
    commit; every tenant-table write takes the matching shared gate and fails
    before mutation with a stable sanitized conflict while erasure is active.
13. AI attachment upload and retention share one size/SHA-verified bounded
    inventory parser and format allowlist. Every accepted PDF/DOCX/XLSX body,
    metadata, relationship, comment, annotation/form value, header/footer, and
    document-property surface must be inventoryable; unsupported legacy content
    makes erasure fail closed.
14. Legacy private-file cutover is an explicit operator operation. It globally
    proves one resource owner per normalized source under a writer fence,
    validates/scans and digest-matches every promoted object, commits registry,
    resource, usage, and deletion-outbox state atomically, and never deletes a
    source itself. Source cleanup occurs only through the digest-bound atomic
    deletion worker after backup and application verification.
15. Candidate merge moves registered object ownership and includes affected
    registry rows in the encrypted reversal snapshot. Candidate erasure scrubs
    subject linkage from every registry lifecycle state and cannot report
    completion while an outstanding changed legacy source remains unresolved.

## Technical limits

| Resource | Pilot synchronous limit | Larger workload |
|---|---:|---|
| Candidate document | 15 MiB | rejected with current usage/limit |
| Sourcing file | 10 MiB | durable import job |
| Import rows | 500/request | durable import job |
| Message recipients | 100/request | durable delivery job |

`entitlement_exceeded` denotes a contract limit; `limit_exceeded` denotes a
technical limit. `null` AI budget is unlimited, zero blocks execution, and a
positive value is an atomic hard cap.

## Delivery status

| Slice | State | Exit evidence |
|---|---|---|
| Contract and baseline | candidate complete | canonical contracts, current 360-operation generated OpenAPI client/catalog parity, `e18c4f6a0b32` migration graph, and E18 candidate evidence ledger |
| Security/data foundation | internally complete | operation/field policy, nine forced-RLS tests, 112/112 tenant-table write gates, two-tenant canaries, readiness redaction, derived erasure, document/PDF boundaries, legacy cutover/deletion, retention tombstones, and candidate-merge ownership pass. The protected run migrated and reconciled 62/62 files, verified backup/restore and audited download, then drained 62/62 redundant sources without retry/DLQ/tombstone. Exact final-revision CI remains a release gate, not unfinished cutover work. |
| Application/assessment domain | complete | 1,158 preserved applications, native assessments, structured-hiring and RU/EN golden journey |
| Durable runtime/AI/connectors | complete (simulated) | hard-kill/reclaim/exact-once drill, AI gateway tests, signed communication/source callback contracts, and outage/replay simulators |
| UX/accessibility/operations | locally complete | generated-contract, typecheck, and production-build checks pass; exact-current four-project web suite collected 124 with 115 passed, 9 intentional project-scope skips, and 0 failed; focused UI recovery passes 12/12 plus 8/8 adjacent; strict real-stack Chromium/Firefox passes 2/2; 17 alert names and live firing/resolved delivery pass; pinned Linux WebKit remains CI-only |
| Adversarial release loop | locally complete | exact final-tree API/database/integration run collected 855: 845 passed, 10 deliberate opt-in skips, 0 failed in 1,297.39 seconds on a fresh migrated database with live Valkey/MinIO/ClamAV; nine forced-RLS cases passed separately and the exact-tree 10k gate passed at 175.952 ms read p95 / 51.942 ms mutation p95; focused adversarial, browser, recovery, and supply-chain checks are green locally; exact committed CI/WebKit/images/SBOM/signing remain pending |

## Preserved-data contract

The original `recruitment_v2` database remains untouched and has a protected
pre-completion dump. The preview database is at `e18c4f6a0b32` and reconciles
84 candidates (82 active and 2 soft-deleted), 105 positions, 1,158 legacy
scores, 1,158 applications, 1,158 stage events, 2,316 evaluations, 17
interviews, one note, and 1,280 activities. On 2026-08-24 the protected cutover
validated and migrated all 62 referenced candidate CVs into 62 ready canonical
objects with zero quarantine/unregistered references and an unchanged stable
business projection SHA. The authorized file-only CV returned 200 with
private/no-store/nosniff headers and one sensitive-read audit. A verified
encrypted post-cutover backup/restore preceded a 62/62 digest-bound redundant-
source drain with zero retry, DLQ, failure, or tombstone. Seven unreferenced
legacy-prefix files—two CV and five assessment objects—remain isolated and are
reported only as opaque counts; they were not adopted or deleted. Exact facts
and hashes are in
`docs/evidence/preserved-data-legacy-cutover-e18-2026-08-24.json`.

## External certification and acceptance gates

The following evidence cannot be manufactured from local simulator runs and
blocks a release GO even after the remaining candidate reruns are green:

- live Google Workspace, Microsoft 365, Telegram, and hh.ru certification needs
  the pilot tenant's sandbox/test credentials;
- the OpenRouter credential exposed during development must be revoked and
  rotated, and the operator must record proof that the old credential is
  rejected without recording either secret;
- keyless Sigstore identity is issued only inside the pinned GitHub Actions
  workflow; the exact committed revision must complete that workflow;
- product, security, operations, and legal reviewers must record acceptance
  after reviewing the preserved-data preview and exact-revision evidence.

## Release gates

- No open severity 1 or 2 defects and no known material defect.
- Unit, API, migration, integration, browser, accessibility, visual, security,
  recovery, and production-build suites are green.
- Every API operation is present in generated role/permission allow-deny tests.
- No public path can reach a private file, token, or PII field.
- Preserved legacy private files reconcile one-to-one with canonical registry
  objects, source and business-data hashes remain stable through apply, and the
  digest-bound source-cleanup worker drains without mismatch or orphaning.
- No reachable critical/high dependency or container advisory.
- Expand/contract migration, rollback, tenant backup, and full restore drills
  meet RPO <= 24 hours and RTO <= 4 hours.
- Golden hiring journey passes in RU and EN.
- No placeholder, fake delivery, disconnected control, or misleading capability
  claim is visible.
- Product, security, and operations reviewers record pilot acceptance.
