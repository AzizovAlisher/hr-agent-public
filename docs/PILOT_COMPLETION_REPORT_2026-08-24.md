# Private B2B Pilot Completion Report

**Report date:** 2026-08-24  
**Candidate migration head:** `e18c4f6a0b32`  
**Engineering completion estimate:** **96%**  
**Release decision:** **NOT GO yet**

The repository is at a private-pilot release-candidate stage: the planned product
surfaces are implemented, tested locally, or truthfully capability-gated, and the
preserved customer workspace is running with its prior business records. The 96%
figure measures engineering completion, not the probability of external approval.
The exact-current-tree local regression is green. The remaining work is limited
to CI-only and credential- or reviewer-dependent certification listed below.

## Preview for review

| Item | Value |
|---|---|
| Web application | `http://127.0.0.1:3100` |
| API | `http://127.0.0.1:8100` |
| Reviewer access | The authentication secret is provisioned out of band and stored only in owner-readable `apps/web/.env.local` as `PREVIEW_REVIEWER_PASSWORD`; do not copy it into source, logs, screenshots, or evidence. The non-secret reviewer email is available in preserved-data evidence and `PREVIEW_REVIEWER_EMAIL`. |
| Preview database | `recruitment_v2_preview` |
| Expected visible data | 82 active candidates and 105 positions |

The preview addresses are available only on the local machine. A useful manual
check is to sign in, open Candidates and Positions, switch between RU and EN,
open a candidate with a CV and application history, and confirm that unavailable
AI/provider controls are hidden or explain their unavailable state instead of
pretending to work.

## Preserved-data proof

The original `recruitment_v2` source database was not targeted. Work was rehearsed
and applied to the protected preview after a validated PostgreSQL archive was
created. Immediate pre/post counts reconcile as follows:

| Record | Preserved count |
|---|---:|
| Organizations / users | 1 / 2 |
| Candidates | 84 total: 82 active, 2 soft-deleted |
| Positions | 105 |
| Legacy scores / canonical applications | 1,158 / 1,158 |
| Application stage events | 1,158 |
| Application evaluations | 2,316 |
| Interviews | 17 |
| Candidate notes / activities | 1 / 1,280 |

Integrity evidence:

- Candidate business projection SHA-256 remained
  `9ace90e32b75d2eadc11776ded64d9820de282dba32bbb98cd803fa2e15763a4`.
- The legacy source inventory contained 69 objects totaling 36,467,423 bytes;
  aggregate SHA-256 was
  `52b58bde49fc77ee928f0d838fd097315858be0916cb6eb46ce58c591bc7035b`.
- All 62 referenced legacy files were validated, malware-scanned, digest-matched,
  migrated, and reconciled to 62 ready canonical private objects. There were zero
  failed, quarantined, or unregistered references.
- The apply rerun was idempotent: zero new scans and zero duplicate effects.
- An authorized CV download returned 200 with `private, no-store`, `nosniff`, and
  a sensitive-read audit event.
- Only after a verified encrypted backup/restore, the cleanup worker drained all
  62 digest-matched redundant source copies with zero retry, dead letter, failure,
  or tombstone. Canonical objects and backups remain retained.
- Seven unreferenced opaque files (two CV, five assessment) remain isolated and
  untouched for separate operator disposition; no paths or content are disclosed.
- The post-cutover recovery drill verified 62 objects / 33,463,197 bytes with
  matching checksums, rollback, RPO 46.885 seconds and RTO 2.804 seconds, both well
  inside the pilot targets of 24 hours and 4 hours.

Source evidence: [preserved cutover record](./evidence/preserved-data-legacy-cutover-e18-2026-08-24.json),
[post-cutover recovery drill](./evidence/post-cutover-e18-recovery-drill-2026-08-24.json),
and [E18 data reconciliation](./evidence/data-preservation-e18-2026-08-24.json).

## What is new and what changed

| Area | Delivered result |
|---|---|
| Identity and access | Admin invitation/provisioning, password reset, admin TOTP, login throttling, revocable rotating sessions, secure logout, disabled-user enforcement, and concurrent last-admin protection. |
| Authorization and tenancy | Server-authoritative operation/action and sensitive-field policy, assignment-aware access, API-key scopes, permission-driven navigation, fail-closed unknown routes, non-owner database roles, and PostgreSQL forced RLS. |
| Applications and candidate CRM | First-class applications with stage events/evaluations and a reconciled 1,158-row legacy compatibility path; multi-position applications, normalized contacts, cursor pagination, bounded duplicate groups, audited reversible merge, and policy-filtered exports. |
| Requisitions and structured hiring | Validated lifecycle and approval evidence, compensation policy, versioned interview kits/scorecards, interviewer assignment scope, blind feedback, decision reasons, offers, and handoff. |
| Native assessments | Versioned templates, sections and questions; invitations, expiry, autosave/resume, accommodations and retakes; deterministic/manual grading; knockout review flags; and native yes/no, choice, number, text, private-file and URL work-sample questions. |
| Communications, scheduling and sourcing | Durable provider jobs, delivery state, retry/DLQ and idempotency; Google/Microsoft calendar/email, Telegram, hh.ru, Somon tracked-link/import, CSV/XLSX and signed REST/webhook contracts. Simulator and failure contracts are implemented; live certification is not claimed. |
| Automation | Versioned permission-gated rules, dry-run, durable execution, replay/pause, quotas, retries, idempotent external effects, and creator reauthorization during worker execution. |
| AI | One tenant-aware gateway for chat/scoring/drafting/missions; managed OpenRouter opt-in, allowlisted private endpoints, SSRF controls, structured output, provenance, human override, atomic budget semantics, sanitized logging, and a capability endpoint so UI controls reflect actual route/opt-in/budget availability. AI never makes irreversible hiring decisions. |
| Analytics | Application-event funnels, conversion and time metrics, offers, source and assessment quality, workload, integration health and AI usage with reconciled filters, drilldowns and exports. |
| Public experiences | RU/EN application, referral, scheduling, shortlist, offer and assessment flows with abuse controls, non-enumerating responses, hashed expiring/revocable tokens, replay protection and privacy/cache/SEO headers. |
| Enterprise and limits | Contract entitlements and usage ledger, manual tenant provisioning, scoped keys, signed outbound webhooks, retention/legal-hold controls, model residency, audit/portability surfaces, and shared pilot limits. |
| Operations | Separate API/worker model, PostgreSQL jobs/outbox, private object storage, Valkey for non-authoritative limits/cache, dependency-aware readiness, metrics/traces/alerts, recovery and rollback tooling, non-root images, CI security gates, SBOM/signing workflows, and runbooks. |

## Final hardening completed in this wrap

- Removed private-file static exposure and introduced a canonical registry,
  quarantine/validation/scan/promotion flow, authorized audited download, and
  atomic digest-bound legacy cleanup.
- Added structural PDF, DOCX/XLSX, OGG, WebM and MP3 validation, size/SHA rechecks,
  safe URI handling, parser limits, zero-residue failures, and fail-closed legacy
  retention behavior.
- Serialized same-tenant writes against erasure and covered all 112 tenant-table
  write gates; 113 protected tables have forced RLS.
- Made candidate merge transfer private-object ownership, include it in encrypted
  reversal evidence, reject invalid ownership graphs, and detect old aliases even
  when assignment-scoped ORM loaders would hide them.
- Hardened sensitive-read and mutation audit behavior under actor disable/delete
  races and pool pressure; requests fail closed instead of succeeding unaudited.
- Redacted communication-account configuration from all list projections even
  when a legacy permission override is present; create/rotate remain one-time
  privileged disclosures.
- Separated retryable private-model DNS failures from terminal configuration
  failures and made candidate/position AI controls depend on sanitized tenant
  capability, field policy, action policy and budget state.
- Corrected candidate/position recovery and permission-driven UI states,
  communication-account read policy, keyboard/accessible interactions, mobile
  behavior, RU/EN copy, and generated OpenAPI TypeScript contracts.
- Extended CI time budgets and the security/supply-chain gates so an exhaustive
  job must actually finish rather than being treated as successful after timeout.
- Added a hash-bound 63-record defect traceability index and adversarial schema
  tests. The validator proves structural consistency only and explicitly cannot
  self-certify test execution or release readiness. A separate secret-free
  pending template documents the provider, credential-incident, hosted-artifact,
  and named-approval evidence that must be completed and signed externally.

## Verification available at report freeze

| Gate | Evidence now |
|---|---|
| Exact final API/database/integration suite | 855 collected: 845 passed, 10 deliberate opt-in skips, 0 failed in 1,297.39 seconds (21m37s), on a fresh database migrated cleanly to `e18c4f6a0b32` with live Valkey, MinIO, and ClamAV. The nine forced-RLS skips passed 9/9 on a separate fresh forced-RLS database; the opt-in 10k performance skip passed separately. |
| Previous full API baseline | 737 collected: 727 passed, 10 deliberate skips, 0 failed in 840.12 seconds; this is regression context, not a substitute for the exact final result above. |
| Authorization / tenant isolation | The current generated OpenAPI and canonical authorization catalog contain the same 360 operations. Nine forced-RLS tests pass; 113 protected tables and 112/112 privacy write gates are covered. |
| Current focused adversarial fixes | Audit reliability/race, communication redaction, merge-chain and assignment-hidden regressions passed 9/9 independently. |
| Private files / privacy | Protected cutover passed 62/62; focused/adjacent cutover coverage 37 passed, 1 environment-gated skip; retention/privacy/object-worker coverage 114 passed, 1 environment-gated skip; candidate merge 10/10. |
| Document/media safety | Final document inventory matrix 133/133 plus 33/33 direct probes; preserved PDFs validate. Current media/PDF/private-LLM focused coverage passed 58, candidate processing passed 31, and storage/retention/security passed 33 with one environment-gated skip. |
| Web | Generated contracts, typecheck and Next production build are clean. The exact-current local four-project browser matrix collected 124 with 115 passed, 9 intentional project-scope skips, and 0 failed; focused recovery passed 12/12 plus 8/8 adjacent, with zero serious/critical axe findings. |
| Real application | Strict actual-stack Chromium and Firefox journeys passed 2/2 using FastAPI, PostgreSQL, Valkey, MinIO, worker and Next.js; a post-hardening isolated Chromium rerun also passed. Linux WebKit remains CI-only. |
| Performance | Exact-tree 10k gate passed: indexed read p95 was 175.952 ms against 500 ms; local mutation p95 was 51.942 ms against 1 second, across 30 runs each. |
| Recovery | Recovery harness passed 36 with 3 opt-in skips; protected post-cutover backup/restore and rollback passed with matching private-object hashes. |

No severity-1 defect remains open, and the exact local suite above is green. The
external gates below must still close before anyone records a release GO.

## External-only launch gates

These are not missing local implementation and cannot be truthfully completed
without credentials, hosted identity, or named human decisions:

1. Run the exact committed revision through GitHub Actions, including pinned
   Linux WebKit, release image builds/scans, dependency/license gates, SBOMs and
   keyless Sigstore signing.
2. Certify Google Workspace, Microsoft 365, Telegram and hh.ru against
   pilot-owned sandbox/test credentials. Somon remains tracked-link/import only;
   no scraping or unapproved deep API is claimed.
3. Revoke and rotate the development-exposed OpenRouter credential and retain
   proof that the old value is rejected, without placing either secret in logs
   or repository evidence.
4. Obtain named product, security, operations and legal acceptance of the
   preserved-data preview, RU/EN journeys, operational evidence and contractual
   privacy/cross-border behavior.

Until those items pass, this candidate is suitable for local stakeholder review
and credentialed certification preparation, but it is not authorized for pilot
production traffic.

## Changed modules and review map

The completion branch is a large product consolidation (665 files changed
relative to `main` in the final working tree). Review by subsystem:

- **API domain and policy:** `apps/api/app/auth/`, `apps/api/app/routers/`,
  `apps/api/app/services/`, `apps/api/app/models/`, and `apps/api/app/schemas/`.
- **Database and tenancy:** `apps/api/alembic/`, `apps/api/app/db.py`, migration
  and forced-RLS/write-gate tests.
- **Files/privacy:** `apps/api/app/storage/`,
  `apps/api/app/services/private_object_retention.py`,
  `apps/api/app/services/candidate_merge.py`, and
  `apps/api/app/scripts/migrate_legacy_private_objects.py`.
- **Durable runtime and integrations:** worker/job/outbox services, connector and
  webhook routers, AI enterprise gateway, provider simulators, and runtime tests.
- **Frontend:** `apps/web/app/`, `apps/web/components/`,
  `apps/web/lib/generated/api.ts`, `apps/web/lib/permissions.ts`, and
  `apps/web/e2e/`.
- **Operations and supply chain:** `.github/workflows/`, `ops/observability/`,
  `ops/recovery/`, `ops/postgres/`, `ops/minio/`, and provisioning contracts.
- **Release evidence:** `docs/`, especially the
  [feature matrix](./CANONICAL_FEATURE_MATRIX.md),
  [acceptance plan](./ACCEPTANCE_TEST_PLAN.md),
  [defect ledger](./DEFECT_LEDGER.md),
  [release contract](./PILOT_RELEASE_CONTRACT.md), and `docs/evidence/`.
  The [external-gate procedure](./EXTERNAL_RELEASE_GATES.md) explains why its
  committed template remains pending and cannot authorize release.

## Decision summary

- **What can be reviewed now:** the restored/preserved local product, complete
  private-pilot workflows, RU/EN UI, accurate capability states, data integrity,
  security behavior, recovery evidence and local automated results.
- **What is not being claimed:** production release GO, live provider
  certification, hosted Linux WebKit/signing success, or human/legal acceptance.
- **Next decision:** run the frozen revision through the hosted gates, certify
  providers with customer credentials, and record the four named approvals.
