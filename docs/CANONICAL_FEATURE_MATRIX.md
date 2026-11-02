# Canonical Private-Pilot Feature Matrix

This matrix is the product and engineering source of truth for the private B2B
pilot. It records the current E18 candidate whose repository head is migration
`e18c4f6a0b32`;
`PILOT_RELEASE_CONTRACT.md` defines the release boundary;
`ACCEPTANCE_TEST_PLAN.md` defines the proof required to change a row to
`accepted`. API behavior, generated clients, UI capability states, runbooks,
and contract entitlements must agree with this file.

Statuses are `implementation`, `verification`, `accepted`, or `deferred`.
`deferred` is valid only for an explicit launch-boundary exclusion; hidden or
decorative controls are not a substitute for implementation.

`accepted` means that the named software capability has local automated and
adversarial evidence. It does **not** mean that the E18 candidate is a release
GO. Exact-revision CI, credentialed provider certification, credential-incident
closure, and human acceptance remain release-wide gates listed below.

## Product capabilities

| ID | Area | Pilot contract | State | Required proof |
|---|---|---|---|---|
| IAM-01 | Identity | Admin provisioning and invitation, reset, admin TOTP, throttled login, rotating/revocable sessions, secure logout, disabled-user rejection, and concurrent last-admin protection. | accepted | AUTH suite and cross-replica session/revocation drill |
| AUTHZ-01 | Authorization | Exact operation catalog, canonical action overrides, field policy, assignment scope, permission-driven UI, API-key scopes, and fail-closed unknown routes. | accepted | Current 360-operation generated catalog parity, nine forced-RLS tests, and the exact final-tree API suite with 855 collected, 845 passed, 10 deliberate skips, and 0 failed |
| TEN-01 | Tenancy | Runtime PostgreSQL role is non-owner/non-bypass; every tenant relation has forced RLS; nested relations have tenant FKs; all reads, writes, files, exports, jobs, AI records, and public capabilities are tenant-bound. | accepted | nine forced-RLS tests and live two-tenant path/body/search/file canaries |
| REQ-01 | Requisitions | Valid lifecycle, separation of request/approval where configured, append-only approval evidence, compensation field policy, pause/close/reopen semantics. | accepted | RU/EN golden journey plus transition/concurrency coverage |
| APP-01 | Applications | First-class application, stage history, owner/source/due/disposition, assessment/interview/offer links, one active candidate-position application, and one-release Score compatibility. | accepted | 1,158-row backfill reconciliation and multi-position/duplicate journey |
| CRM-01 | Candidate CRM | Normalized contact points, search/notes/tags/pools/nurture, dedupe, audited reversible merge, cursor lists, and policy-filtered exports. | accepted | 10k gate, reversible merge, bounded 3k duplicate component, pagination/export reconciliation |
| HIRE-01 | Structured hiring | Versioned kits/scorecards, interviewer assignments, blind feedback, required answers, keyboard alternatives, decision reasons, offers, and audited handoff. | accepted | assigned/unassigned matrix and end-to-end golden journey |
| ASM-01 | Native assessments | Immutable templates/versions/sections/questions, invitations, one-attempt default, approved retake, expiry, accommodations, autosave/resume, grading, results, knockout review flag, and stage linkage. | accepted | all native question/rule states plus public attempt golden journey |
| ASM-02 | Question types | Yes/no, single choice, multi-choice, number, short text, long text, private file, and URL work samples. Conditions reference only prior questions with type-safe `all`/`any` operators. | accepted | deterministic branch/validation/property cases |
| COM-01 | Communications | Sandbox is explicit; provider work is durable with status, idempotency, bounded retry/DLQ, opt-out, reminders, and partial-failure inspection. Sandbox/provider callbacks use signed versioned envelopes, bounded bodies, timestamp validation, and replay-safe event fingerprints. | verification | signed callback and simulator contracts are accepted; live Google/Microsoft/Telegram certification awaits pilot credentials |
| CAL-01 | Scheduling | Google Workspace and Microsoft 365 OAuth calendar operations, token refresh, app-scoped disconnect, provider-backed timezone/DST conflict checks, durable reminders, and expiring/revocable management links. Microsoft disconnect deletes local encrypted tokens because Graph has no app-scoped revoke endpoint. | verification | simulator and browser matrix accepted; live tenant OAuth certification awaits credentials |
| SRC-01 | Regional sourcing | hh.ru official API, Telegram Bot API, Somon tracked links and validated import, CSV/XLSX preview/mapping, idempotent imports, provenance, health, capability states, and signed replay-safe generic-source webhooks. Somon scraping is prohibited. | verification | signed callback contracts/simulators accepted; credentialed hh.ru/Telegram certification remains external |
| WF-01 | Automation | Versioned permission-gated rules, dry-run, durable runs, external-effect idempotency, retry/DLQ, history, quotas, pause, and replay. | accepted | live SIGKILL, lease reclaim, replay, and exact-once effect drill |
| AI-01 | AI gateway | Every AI path uses the tenant gateway. Private vLLM endpoints are operator-allowlisted; managed OpenRouter is opt-in with disclosure. Structured output, redaction, provenance, usage/cost, timeouts, and human approval are mandatory. | accepted | route inventory, SSRF/prompt-injection/PII/budget race suites |
| AN-01 | Analytics | Event-based funnel/cohort, conversion, time-in-stage/fill/hire, offers, sources, assessments, workload, integration health, and AI usage. Filters, drilldowns, and exports share cohort semantics. | accepted | application-event aggregate/detail reconciliation and golden journey |
| PUB-01 | Public flows | RU/EN application, referral, schedule, shortlist, offer, and assessment flows are accessible, non-enumerating, abuse-limited, no-store/noindex/no-referrer, and use hashed expiring revocable tokens with semantic 404/410. | accepted | abuse/token API coverage plus strict current-source real-stack Chromium and Firefox journeys pass |
| PRIV-01 | Privacy | Data inventory, sensitive-read audit, 30-730 day retention, legal holds, immediate approved live erasure, private-object deletion outbox, provider/cache traversal, and 35-day backup expiry manifests. Tenant writes serialize with erasure, and upload/retention share one bounded content inventory contract. | accepted | derived-graph/marker and erasure-write-gate evidence is accepted; document boundary final matrix passes 133/133 plus 33/33 independent ZIP/PDF/OOXML/no-orphan/legacy/EN-RU direct probes |
| ENT-01 | Enterprise | Accurate capability states, scoped API keys, signed outbound webhooks, audit export, backup export, residency controls, and manual provisioning/contract entitlements. | accepted | scope/signature/replay and capability-state matrix |
| OPS-01 | Operations | Separate API/workers, PostgreSQL durable queues, private object storage, Valkey non-authoritative limits/cache, dependency-aware readiness, metrics/traces/alerts, non-root pinned images, SBOM/signing, backup/restore, and rollback runbooks. Public readiness is redacted; component detail requires `enterprise.manage`. | verification | recovery harness passes 36 with 3 opt-in skips; 15 alert-rule groups/39 assertions cover all 17 names and live firing/resolved delivery passes; local dependency checks are clean; exact-revision images/SBOM/OIDC signing remain required |
| UX-01 | UX/accessibility | Responsive mobile shell/chat, keyboard alternatives, semantic tabs/dialogs/popovers/charts, visible focus, announcements, contrast, reduced motion, touch targets, RU/EN, and actionable retry/partial failure. | verification | clean generated-contract/typecheck/build checks; exact-current four-project local web matrix collected 124 with 115 passed, 9 intentional project-scope skips, and 0 failed; focused recovery 12/12 plus adjacent 8/8 and strict real-stack Chromium/Firefox 2/2 pass; exact pinned Linux WebKit remains CI-only |
| ENTITLE-01 | Limits | Contract entitlements and usage ledger cover seats, jobs, candidates/imports, assessment starts, AI cost/tokens, storage, and integration runs. UI and API share limits and stable errors with usage. | accepted | null/zero/cap and concurrent reservation/settlement coverage |

## Launch-boundary capabilities

| Capability | Pilot state | UI/API behavior |
|---|---|---|
| Public signup and self-service payment | deferred | No route or control; organizations and contracts are provisioned manually. |
| Native coding execution | deferred | Generic assessment-provider capability only; unavailable unless a connected provider advertises it. |
| Psychometric library | deferred | Generic assessment-provider capability only. |
| Native video interviewing | deferred | Not shown as available. |
| WhatsApp delivery | deferred | No pilot-visible send control. |
| Production SSO | deferred | Configuration remains unavailable/capability-gated; it must not claim operational status. |
| Deep Somon.tj publication API | deferred | Tracked links and validated imports only until an official partner API exists. |

## Architecture and shared contracts

```text
Authenticated UI / public candidate UI / API clients
                         |
              Next.js same-origin BFF
                         |
              FastAPI policy boundary
          /              |               \
 PostgreSQL + FORCE RLS  BlobStore       Valkey
 application/events     public/private  limits/cache only
 durable jobs/outbox    object registry
          \              |               /
                  separate workers
             /                           \
      provider connectors           tenant AI gateway
```

- `Application` owns candidate-position lifecycle. `Score` is a one-release
  compatibility representation and must not regain lifecycle authority.
- OpenAPI is the transport source of truth. Only the integration lead exports
  it and regenerates TypeScript clients.
- Errors use `application/problem+json` with stable `code`, `status`,
  `request_id`, and optional field errors. Chat streaming uses typed JSON
  `delta`, `usage`, `done`, and sanitized `error` events with one terminal
  event and exactly-once message persistence.
- Collections use stable cursor pagination. Public submissions and external
  effects require stable idempotency keys.
- Private objects are quarantined, type/size validated, malware-scanned,
  atomically promoted, registry-bound, tenant/field authorized, audited on
  sensitive reads, and removed through the deletion outbox.
- Candidate erasure holds an exclusive tenant advisory lock from inventory
  through commit. Every tenant-table write takes the matching shared lock and
  fails before mutation with the sanitized `privacy_erasure_in_progress`
  problem if erasure owns it; the schema gate covers 112/112 tenant tables.
- AI attachment upload and later retention must use the same size/SHA-verified
  bounded parser and format allowlist. Every accepted PDF/DOCX/XLSX body,
  metadata, relationship, comment, annotation/form value, header/footer, and
  document-property surface must be inventoryable; unsupported new content is
  rejected and unsupported legacy content fails closed during retention.
- Malware readiness uses the digest-pinned ClamAV 1.4.6 compatibility target,
  exercises clean and EICAR behavior, and fails closed when signature age is
  greater than the fixed 48-hour readiness/alert threshold.
- Connector states are `unavailable`, `disconnected`, `connecting`,
  `connected`, `degraded`, `expired`, and `revoked`. Signed webhooks use a
  versioned envelope, HMAC-SHA256, timestamp/replay validation, idempotent event
  IDs, and inspectable retry history.
- Public `GET /health/ready` returns only status and check time with
  `no-store`; detailed dependency state is available only from authenticated
  `GET /enterprise/operations/readiness` with `enterprise.manage`.
- AI budget `null` is unlimited, `0` blocks, and positive values are hard caps
  reserved atomically then settled/refunded. AI can recommend or draft, never
  make an irreversible hiring decision.
- Pilot limits are 15 MiB candidate documents, 10 MiB sourcing files, 500
  import rows per synchronous request, and 100 synchronous recipients. Larger
  supported workloads become durable jobs.

## Authority and change control

When artifacts disagree, precedence is: this matrix and the release contract,
the operation/field policy catalog, the OpenAPI document, generated clients,
then UI copy. A feature change must update this matrix, authorization, tests,
telemetry, RU/EN copy, and capability state in the same vertical slice.

## E18 candidate release-wide gates

The E18 candidate remains **NOT GO** until all of the following are attached to
one immutable revision:

- retain the completed `e18c4f6a0b32` preserved-data reconciliation, canonical
  private-object cutover, authorized-download, backup/restore, rollback, and
  source-drain evidence on the release revision;
- exact-revision CI, artifact, SBOM, and keyless-signing evidence;
- live Google Workspace, Microsoft 365, Telegram, and hh.ru certification with
  pilot-owned sandbox/test credentials;
- revocation and rotation proof for the OpenRouter credential exposed during
  development, including proof that the old credential is rejected;
- independent product, security, operations, and legal acceptance; and
- the pinned Linux WebKit real-stack journey on the exact committed revision.
