# Private-Pilot Acceptance Test Plan

This is the executable release checklist for the canonical feature matrix. A
test family is accepted only when its automated suite is green, required manual
evidence is attached to the defect ledger, and an independent verifier records
the defect and adjacent-flow retest. Implementation alone never closes a gate.

## Required fixtures

- Organizations `alpha` and `beta` contain colliding candidate names, email
  addresses, external IDs, object names, and idempotency keys.
- Each tenant has admin, recruiter, hiring manager, interviewer, restricted,
  disabled, and scoped API-key identities. Include explicit allow and deny
  overrides for every canonical action.
- Data profiles are empty, ordinary, and 10,000 candidates. Include duplicate
  people, one person applying to multiple positions, expired records, legal
  holds, long chats, simultaneous stage changes, and nearly exhausted quotas.
  Include registered, unregistered, duplicated, cross-tenant, changed,
  symlinked, missing, already-deleted, and dead-lettered legacy private-object
  references plus merge/reversal snapshots on both sides of the cutover fence.
- Capability tokens are valid, malformed, expired, revoked, rotated, consumed,
  and concurrently replayed.
- Every connector state is represented. Simulators produce success, timeout,
  disconnect, invalid credential, refresh expiry, 429 with retry hints, 5xx,
  duplicate/out-of-order webhook, signature failure, and recovery.
- AI settings cover private-only routing, managed-provider opt-in/out, budget
  `null`, zero, below-estimate, exact, and concurrent last-unit reservations.
- Browser matrix: 320x568, 390x844, 768x1024, 1024x768, and 1440x900 in current
  Chromium, Firefox, and WebKit; RU/EN, light/dark, reduced motion, keyboard,
  touch, slow network, offline/reconnect, and 200%/400% zoom.

## Acceptance families

| ID | Family | Passing condition |
|---|---|---|
| E2E-001 | Golden hiring journey | Requisition -> approval -> position -> sourcing/public apply -> dedupe -> application -> assessment -> scoring -> interview -> complete scorecards -> offer -> acceptance -> handoff/hire -> analytics succeeds in RU and EN without direct database repair. |
| AUTH-001 | Login/session/MFA | Generic failures, password boundaries, distributed throttling, TOTP replay/window behavior, expiry, rotation reuse detection, password/role/disable revocation, secure logout, and concurrent last-admin protection pass. |
| AUTHZ-001 | Operation matrix | The live route table exactly equals the reviewed operation catalog. Every bearer operation is exercised for default roles and explicit allow/deny overrides; API remains authoritative when UI requests are forged. |
| TEN-001 | Tenant isolation | Cross-tenant IDs in paths, queries, bodies, nested relations, search, files, exports, jobs, AI sessions, API keys, and capabilities reveal no object and make no mutation. Live runtime role is non-owner/non-bypass and every tenant relation has enabled/forced RLS and its policy. |
| FILE-001 | File safety | Anonymous/wrong-tenant downloads fail. Oversize, MIME mismatch, malformed PDF/Office, polyglot, active SVG, archive bomb, traversal name, malware, scanner outage, interrupted promotion, and duplicate upload leave no reachable/orphan private object. Authorized reads are audited and no-store. |
| PRIV-001 | Erasure/retention | A unique boundary-safe identifier canary is removed from live/derived database rows, private objects, parsed content, AI/chat/workflow payloads, communications, exports, provider deletion records, caches, and backup-expiry manifests without deleting common-name/subsequence controls. Legal holds prevent deletion and are audited. Same-tenant writes serialize against erasure and fail with the stable `privacy_erasure_in_progress` problem before mutation. |
| FILE-002 | Attachment retention parity | Upload and later retention use the same size/SHA-verified bounded parser and format allowlist. Every accepted TXT/CSV/PDF/DOCX/XLSX body, metadata, relationship, annotation/form field, header/footer/comment, and document-property surface is inventoryable; unsupported content is rejected for new uploads. Rejected/interrupted uploads leave neither registry row nor blob, and unsupported legacy content makes erasure fail closed without deleting the candidate or object. |
| FILE-003 | Legacy private-object cutover | Default dry-run is non-mutating; global ownership preflight, operator/RLS guard, supported-writer fence, semantic validation, fresh malware scan, source stability, atomic registry/resource/usage/deletion binding, zero-entitlement migration, cleanup, and idempotent rerun pass for every resource category. A tenant-filtered run still rejects a cross-tenant alias. Real preserved-data apply leaves all business rows and source bytes unchanged until a verified backup and worker drain; promoted object size/SHA and authorization reconcile exactly. |
| FILE-004 | Legacy deletion/retention identity | The worker atomically claims only the digest-bound local inode and never deletes a concurrent replacement or follows a late symlink; crash tombstones resume. Candidate erasure rejects a changed outstanding cutover source, adopts and reactivates matching replacement rows, and scrubs subject linkage/metadata from ready, pending, and already-deleted registry tombstones before reporting completion. |
| DOM-003 | Candidate merge object ownership | Merge moves, never copies, a registered or legacy CV binding to an empty primary; distinct documents retain one exact owner and repeated legacy aliases are cleared. Registry ownership, candidate columns, encrypted snapshot digests, reversal, divergence, and cross-tenant/mismatched/private-shaped failure cases are deterministic under the cutover fence. |
| PUB-001 | Public abuse/tokens | Apply is generic and idempotent under replay/concurrency; abuse limits prevent enumeration, spam, oversized bodies, notification amplification, and AI/provider cost. Tokens hash, expire, revoke, rotate, and yield semantic non-cacheable 404/410 responses. |
| ASM-001 | Native assessments | Every question type, earlier-answer branch, `all`/`any`, required rule, partial answer, knockout review flag, timer, autosave/resume, expiry, one-attempt default, approved retake, accommodation, deterministic/manual grade, file/URL evidence, and callback replay passes. |
| AI-001 | AI safety/governance | Route inventory proves every AI feature uses the gateway. Prompt-injection cannot escape policy; PII canaries do not enter general logs or wrong providers; SSRF/redirect/DNS-rebinding/private/link-local/metadata targets fail; budgets are race-safe; provenance and human overrides persist. |
| FAIR-001 | Paired AI evaluation | Paired resumes differing only in agreed protected-trait markers remain inside the pilot customer's recorded tolerance, with model/prompt version and human review evidence. This is an evaluation gate, not proof of universal fairness. |
| INT-001 | Connectors | Connect, OAuth state/PKCE, refresh, revoke, health degradation/recovery, idempotent jobs, duplicate/out-of-order signed callbacks, retry/DLQ/replay, rate limit, outage, and secret rotation pass for every mandatory connector simulator. Communication and source callbacks require the raw-body HMAC, timestamp window, versioned event envelope, bounded body, and event-ID/payload-fingerprint replay contract. Live certification is separately recorded per credentialed provider. |
| TIME-001 | Calendar/time | DST gap/fold, attendee timezone, overlap, cancellation/reschedule race, reminder retry, expired/revoked link, and provider reconciliation cases pass. |
| AN-001 | Analytics | Displayed aggregates reconcile to application-event detail across date, timezone, position, source, owner, duplicate, and multi-position filters. Drilldowns and exports reproduce the same cohort. |
| A11Y-001 | Accessibility/visual | Automated axe has zero serious/critical findings; keyboard and screen-reader journeys complete; focus, live announcements, semantic controls, contrast, reduced motion, target sizes, responsive layout, themes, and zoom matrix pass without clipped or unreachable actions. |
| REL-001 | Reliability | Killing API/worker processes loses and duplicates no work; stale leases cannot acknowledge; retries are bounded; DLQ/replay is idempotent; two API/two worker replicas operate correctly; maintenance batches do not starve later tenants. |
| PERF-001 | Performance | With 10k candidates, indexed local reads are p95 <=500 ms and local mutations p95 <=1 s under the recorded pilot load profile. External work remains asynchronous and queue age alerts before SLO breach. |
| OPS-001 | Readiness/observability | Public readiness exposes only `status` and `checked_at`; authenticated `enterprise.manage` readiness verifies database, migrations, runtime RLS role, object storage/scanner, queue age, and worker heartbeat. Metrics/traces correlate request/job/provider IDs and alerts fire and resolve in drills. |
| SUPPLY-001 | Supply chain | Locked installs, typecheck/build/tests, SAST, secret scan, dependency/license/container scans, non-root pinned image, SBOM, and artifact signing pass with no unresolved reachable critical/high finding. |
| REC-001 | Recovery | Expand/contract migration, rollback, tenant backup, and full database-plus-object restore drills pass with RPO <=24 hours and RTO <=4 hours; restored tenant isolation and object hashes are verified. |

## E18 candidate evidence snapshot

This is a point-in-time candidate snapshot, not a release decision. The
repository migration head and preserved-data preview schema are
`e18c4f6a0b32`. Database/domain reconciliation and the protected legacy
private-file cutover, backup/restore, and source drain are complete. The exact
final-tree local suite is green; hosted exact-revision CI and external release
gates remain pending.

| Gate | Current evidence | Candidate disposition |
|---|---|---|
| API ordinary suite | Exact final tree on a fresh database at `e18c4f6a0b32`, with live Valkey/MinIO/ClamAV gates: 855 collected, 845 passed, 10 deliberate opt-in skips, 0 failed in 1,297.39 seconds; the nine forced-RLS skips passed 9/9 separately and the opt-in 10k skip passed separately | pass |
| Forced tenant RLS | 9/9 passed; schema gate covers 112/112 tenant tables | pass |
| Live storage/limits | 3 passed against real MinIO/versioned erasure and distributed Valkey | pass |
| OpenAPI/client | 360 operations with exact canonical authorization-catalog parity; artifact current; 30/30 boundary tests; 0 handwritten wire DTOs; 307 generated-backed and 172 explicit UI-only declarations | pass |
| Web/type/build/browser | Generated contracts 30/30 including suffix parity, zero handwritten DTO residual, clean typecheck and Next 16.3.2 production build; the exact-current four-project local matrix collected 124 with 115 passed, 9 intentional project-scope skips, and 0 failed | pass locally; Ubuntu WebKit remains CI authority |
| Real-stack browser | 2/2 strict isolated journeys: one Chromium and one Firefox, using actual FastAPI/PostgreSQL/Valkey/MinIO/worker/Next services | pass locally |
| Linux WebKit | Exact Playwright 1.62.1 job is checked in for the same isolated journey | CI-only; not executed locally on this candidate |
| UI recovery/accessibility | Focused recovery states 12/12 plus 8/8 adjacent checks; axe has zero serious/critical findings | pass locally |
| Privacy write gate | Migration round-trip/check and 13 migration guards pass; five problem-contract cases pass; same-tenant writes fail before mutation while adjacent tenants and post-release writes succeed | pass locally |
| Attachment/retention parser | Independent final matrix passes 133/133 plus 33/33 direct ZIP, no-orphan, size/SHA/TOCTOU, legacy atomic-stop, and safe EN/RU probes. Exact-500, lexical XML, Word fields/controls, Unicode PDF font graphs, interactive/unreachable/trailing PDF, TXT/CSV/import, and erasure cases are green | pass independently; DEF-043 closed |
| Semantic PDF compatibility/safety | Focused semantic action/revision tests distinguish inert font/image-stream bytes from reachable actions; all 49 preserved PDFs validate, while JavaScript/launch/mutating/unknown/automatic actions, unsafe URIs, embedded/interactive surfaces, and appended payloads remain rejected | focused and exact final-tree integrated API suites pass |
| Legacy private-object cutover | Protected dry run validated 62/62; apply migrated 62/62; idempotent rerun scanned 0; 62 ready bindings and zero quarantine/unregistered references reconciled; authorized audited download passed; encrypted backup/restore met RPO/RTO; worker drained 62/62 with zero retry/DLQ/tombstone; final global dry run reports only seven opaque unreferenced files | pass internally; focused/adjacent suite passed 37 with one environment-gated skip and independent review found no material issue; exact final-revision CI remains separate |
| Retention/private-object tombstones | Independent isolated and adjacent privacy/object-worker matrix covers changed sources, dead-letter reactivation, source drain, live objects, and deleted tombstones | pass internally: 114 passed, one environment-gated skip; preserved drain completed without mismatch |
| Candidate merge object ownership | Registered and legacy CV ownership moves, distinct/repeated document behavior, registry/path/tenant identity, reversal, corruption, and divergence are covered | pass internally: 10/10 focused cases and independent adjacent review with no material finding |
| Performance | Exact-tree 10k candidate indexed reads p95 175.952 ms; local mutations p95 51.942 ms across 30 runs each | pass against 500 ms/1 s targets |
| Recovery harness | 36 passed, 3 opt-in skips; disposable Docker tenant backup/restore/rollback drill separately 1 passed | pass locally |
| Observability | Prometheus config/rules pass; 15 test groups/39 assertions cover all 17 alert names; six-panel dashboard, authenticated OTLP/TLS, and live firing/resolved delivery pass over 156 scrapes | pass locally |
| Supply chain | pip-audit 101/0; API licenses 97/0; Bandit 88,459 LOC with 0 high and 10 reviewed medium; web audit 0 and 61/0 production licenses; Trivy secrets 768 source files/0 high-critical; compile 383/0; 5/5 Docker base stages and 22/22 actions pinned; runtime UID:GID 10001 | pass locally; floating `apk upgrade` makes exact image scan/SBOM/signing CI-authoritative |
| Preserved preview | 1 organization, 84 candidates (82 active, 2 soft-deleted), 105 positions, 1,158 scores/applications/stage events, 2,316 evaluations, 17 interviews, 1 note, and 1,280 activities; all 62 referenced CVs are ready canonical objects and the stable candidate business projection SHA is unchanged | preserved-data cutover/authorized download/backup/restore/drain pass; original `recruitment_v2` remains untouched; seven opaque unreferenced files (two CV/five assessment, no paths retained in evidence) await separate disposition; exact final-revision CI and external acceptance remain pending |

## Evidence protocol

Every defect/evidence record contains:

1. acceptance ID, build revision, environment, tenant, identity/role, locale,
   device/browser, and preconditions;
2. exact reproduction and expected/actual result with sanitized logs,
   screenshots, trace/request/job IDs, or SQL assertions;
3. severity, affected contract, owner, fix revision, and regression test;
4. independent retest and at least one adjacent-flow retest; and
5. final disposition: closed, deferred with launch-boundary rationale, or open.

Severity 1 includes cross-tenant or public PII/file access, authentication
bypass, irreversible corruption/loss, arbitrary code execution, or a complete
golden-journey outage. Severity 2 includes permission bypass, material privacy
failure, duplicated external effects, persistent inaccessible core workflow,
or recovery/SLO failure.

## Automated release commands

The CI workflow is authoritative; local evidence must run equivalent locked
commands against an explicitly named `*_test` database:

```text
API: locked dependency sync; Alembic upgrade/check and expand/contract drill;
     full pytest; forced-RLS database suite; pip-audit; Bandit.
Cutover: protected backup verification; non-mutating legacy-object dry run;
     applying run; per-object/source/database reconciliation; idempotent rerun;
     authorized/wrong-tenant/file-only UI smoke; post-migration backup; atomic
     deletion drain; final reconciliation.
Web: frozen pnpm install; OpenAPI generated-client drift check; TypeScript;
     production build; browser/a11y/visual suites; strict real-stack
     Chromium/Firefox and pinned Linux WebKit journeys; pnpm audit.
Image: build; non-root assertion; vulnerability scan; SBOM; signature verify.
Recovery: tenant backup/restore and full database-plus-object restore drill.
```

No command may derive a destructive database target from an unresolved
environment variable. Test database names must end in `_test`.

## Release decision

Release is `GO` only when all acceptance families pass, there are no open
severity-1/2 or other known material defects, private file/token/PII reachability
is zero, RU and EN golden journeys pass, live restore/rollback evidence exists,
and product, security, and operations reviewers sign acceptance. Missing third-
party credentials may defer only the named live-provider certification; local
simulators, contract tests, and all non-credentialed implementation remain
mandatory.

For E18, release also requires proof that the development-exposed OpenRouter
credential was revoked and rotated and that the old credential is rejected.
Credentialed Google Workspace, Microsoft 365, Telegram, and hh.ru certification
and independent product, security, operations, and legal acceptance remain
external gates. None may be inferred from local simulator success.
