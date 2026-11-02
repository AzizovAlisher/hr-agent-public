# Candidate Data Classification and Erasure Inventory

Status: canonical private-pilot inventory for `PRIV-001`
Backup maximum age: 35 days
Live erasure boundary: one PostgreSQL transaction plus durable private-object
and provider-deletion work

## Contract

Candidate data is confidential tenant data. Contact values, CVs, assessment
answers, work samples, recordings, communications, offers, and AI evidence are
sensitive fields. Capability tokens, provider credentials, and encrypted job or
webhook bodies are restricted secrets.

An approved erasure must either:

1. remove or pseudonymize every candidate-bearing live copy and durably stage
   physical deletion in the same transaction; for specialist-assessment
   providers, retain the candidate until the durable provider deletion has a
   confirmed receipt; or
2. fail closed and leave the candidate intact.

A legal hold on any owned private object protects the complete candidate merge
closure. A missing encryption key, corrupt/unknown-key ciphertext, ambiguous
calendar create, missing provider connection, or inability to enqueue required
provider deletion makes erasure unprovable and rolls the transaction back.

## PostgreSQL inventory

| Store | Classification | Candidate linkage | Erasure action |
|---|---|---|---|
| `candidates` | confidential/sensitive | authoritative subject root | hard delete after preflight; generated search document and CV fingerprint disappear with the row |
| `applications`, `application_stage_events`, `application_evaluations` | confidential hiring history | candidate FK, then application FK | database cascade |
| `candidate_activities`, `candidate_contact_points`, `candidate_notes`, `candidate_tags`, `scores`, `candidate_processing_jobs` | confidential; contacts/CV job context sensitive | candidate FK | database cascade; queue leases are removed before the job FK disappears |
| `candidate_merge_batches`, `candidate_merge_batch_members` | sensitive identity history and encrypted reversal snapshot | primary/member candidate FK | merge closure is calculated first; database cascade removes batch and snapshot |
| `candidate_handoffs`, `candidate_offers`, `offer_approvals` | sensitive employment/compensation | candidate/offer FK | database cascade |
| `interviews`, `interview_assignments`, `interview_kit_versions`, `interview_questions` | confidential evaluation | candidate/interview FK | database cascade |
| `scorecard_assignments`, `scorecard_ratings`, `hiring_decisions`, `hiring_decision_approvals` | sensitive evaluation/decision | candidate/assignment/decision FK | database cascade |
| `interview_scheduling_links`, link interviewers, `interview_appointments`, appointment interviewers, `interview_reminders` | sensitive schedule/contact/token data | candidate/link/appointment FK | database cascade after external calendar cleanup is durably staged |
| `assessment_invitations`, `assessment_attempts`, `assessment_answers`, `assessment_results`, `assessment_files` | sensitive assessment evidence and work samples | application/invitation/attempt FK | database cascade; file bytes use the private-object path below |
| `provider_assessments`, `provider_assessment_results` | sensitive external assessment evidence and capability URL | candidate/application FK | a confirmed remote-erasure receipt gates candidate deletion; local rows and encrypted evidence then cascade, while linked integration work/webhooks are separately traversed |
| `communication_threads`, `communication_messages` | sensitive contact and message bodies; inbound event IDs and SHA-256 payload fingerprints are restricted replay evidence and are not substitutes for storing a raw signed request | candidate FK | database cascade |
| `talent_pool_members`, `talent_nurture_enrollments` | confidential CRM membership | candidate FK | database cascade |
| `task_submissions`, `interview_recordings` | sensitive work sample/recording | candidate FK | database cascade; bytes use the private-object path below |
| `ai_candidate_source_objects`, `mission_candidate_assessments`, `evidence_claims` | sensitive AI evidence/provenance | candidate FK | database cascade plus containing AI-session traversal |

### Deliberate non-cascade tables

| Store | Why it does not cascade | Required erasure behavior |
|---|---|---|
| `chat_sessions` and `messages` | candidate FK is `SET NULL` so ordinary candidate lifecycle does not destroy chat | candidate-linked session is deleted explicitly, cascading its messages |
| `workflow_runs` | run history survives ordinary subject lifecycle | candidate run, action payload, retry state, and queue lease are deleted explicitly |
| `source_candidate_receipts` | import aggregate must survive ordinary dedupe/lifecycle | candidate receipt, provider external ID, and source URL are deleted explicitly |
| `notifications` | flexible JSON payload has no candidate FK | recursively matched candidate notifications are deleted, including nested IDs |
| `communication_batches.skip_summary` | one batch can contain many candidates | matching entries are removed and requested/skipped counters reconciled |
| `audit_logs` | security history is normally append-only | candidate resource rows and candidate-bearing metadata are removed; the replacement backup-expiry manifest contains only keyed digests |
| `usage_ledger_entries` | immutable deltas are required for correct entitlement totals | amount/kind/time remain; subject resource, idempotency key, and metadata are pseudonymized |
| `worker_queue_leases` | cross-replica leases intentionally have no work FK | leases for candidate processing, workflow, or scrubbed integration jobs are deleted |

## AI prompts, chat, and derived evidence

- General LLM logs retain fingerprints, model/provider, prompt version, usage,
  latency, cost, and fallback provenance; they do not retain raw prompts or
  responses.
- Candidate AI sessions are discovered from attachment provenance, mission
  assessment/evidence links, and a one-release legacy message scan using the
  candidate inventory. The containing `ai_sessions` row is deleted, cascading
  `ai_messages`, `hiring_missions`, plan versions, steps/attempts, agent runs,
  action bundles, events, assessments, and claims.
- Candidate-specific scoring rows and processing jobs cascade from the subject.
  Provider usage ledger rows contain purpose/version metadata only; any row
  with a candidate subject marker is pseudonymized by the live-store sweep.
- No AI output is an authoritative or irreversible hiring decision.

## Private files and parsed content

Private candidate documents, assessment files, work samples, AI attachments,
and interview recordings are registered in `private_objects`. Pre-registry
local files are represented by `legacy_private_object_deletions` during
erasure.

The retention preflight:

1. finds candidate, assessment, task, recording, and AI-source objects;
2. expands merged-candidate roots;
3. excludes a complete root when any owned object is on legal hold;
4. makes registered objects unreadable as `delete_pending` and writes a durable
   deletion outbox row;
5. adopts or durably queues verified legacy local paths;
6. pseudonymizes object/deletion `resource_id` values before commit; and
7. lets the separate deletion workers remove bytes with bounded retry.

The object/deletion tombstone temporarily retains the random object key, byte
count, and SHA-256 integrity value needed to prove the correct object was
deleted. Original filenames, scanner details, and media type are cleared when
physical deletion settles. A queued deletion is not represented as completed
physical deletion.

## Integrations and provider data

`integration_jobs`, `integration_cursors`, `integration_webhook_events`, and
`integration_webhook_deliveries` are scanned while locked. Payloads are opened
only with their original tenant, connection, direction, event/job ID, purpose,
and key ID. Matching webhook rows/cursors are deleted. Matching jobs are
cancelled when runnable and have their references, encrypted payload, result,
idempotency key, and leases scrubbed. Delivery-attempt rows cascade from an
outbound delivery.

Every encrypted artifact in the tenant must be decryptable during this proof;
an unknown/retired key or invalid authentication tag blocks erasure. This is
intentional: silently skipping an unreadable row would create an unmeasurable
privacy orphan.

Calendar events are the exception needed to finish provider erasure. Existing
sync work is cancelled and scrubbed, and a purpose-bound `calendar.delete` job
retains only the encrypted external event ID while runnable. The idempotency
and visible reference use irreversible appointment/event digests. If the create
outcome is ambiguous or deletion cannot be staged, local erasure fails closed.
Retry and dead-letter states retain that purpose-bound ciphertext for replay.
On confirmed terminal success, the integration worker clears ciphertext, nonce,
and key ID atomically with the `succeeded` transition.

Specialist-assessment erasure uses the normalized `assessment.delete` worker
operation. Its encrypted request contains the opaque candidate subject reference
and any known invitation/attempt identifiers; visible references contain only
irreversible digests. The provider contract is `POST /v1/privacy/erasures` and
requires a completed `status: erased` plus a receipt. The application retains
the complete candidate merge root and reports zero candidate deletion while the
operation is queued, running, retrying, dead-lettered, or awaiting an in-flight
invitation. Only confirmed success unlocks local candidate erasure. The receipt
is stored only as SHA-256, and the sensitive job payload is cleared atomically
on success. Failure retains encrypted replay evidence and remains visible in
integration job/DLQ operations.

The local simulator contract covers invitation, attempt, result, callback, and
confirmed privacy erasure. Credentialed sandbox/production certification is
deferred until a pilot provider account is supplied; it must demonstrate the
same completed-erasure response and furnish the processor-side receipt. The
application never treats local row deletion or an HTTP acceptance alone as
proof that vendor data was deleted.

Delivered email, Telegram messages, webhooks already received by a customer,
and data retained by an originating source system cannot be recalled by the
application. They are downstream/controller or processor copies governed by
contractual deletion requests and provider evidence.

## Exports and caches

- Candidate CSV and core-data portability exports are generated in the HTTP
  response and are not stored by the application. A downloaded copy is outside
  live server erasure and must be handled by the customer's export policy.
- There is no server-side export artifact table or export object bucket in the
  pilot.
- Valkey is non-authoritative. It stores sliding-window rate-limit entries with
  HMAC-derived network and capability identifiers and a bounded expiry; it does
  not store candidate contact values, CVs, responses, or candidate rows.
- In-process configuration/model client caches contain tenant configuration,
  not candidate payloads.

## Backup aging and evidence limitation

Every successful live-store preflight writes
`candidate_erasure.backup_expiry_manifest` with:

- keyed, non-reversible candidate digests;
- live-erasure timestamp;
- `backups_not_after` exactly 35 days later;
- `physical_deletion_verified: false`; and
- the two required evidence classes: encrypted-archive prune receipt and
  object-store lifecycle evidence.

This manifest is an expiry obligation, not proof that an archive was physically
deleted. The recovery tooling limits backup retention to 35 days and emits
signed/checksummed receipts with `expires_at`; `prune-local` removes expired
local encrypted archives. `ops/recovery/s3-backup-lifecycle.json` declares a
35-day current and non-current object lifecycle for remote recovery archives.
Application tests can prove the manifest deadline and live-store absence, but
they cannot prove that an operator installed the lifecycle policy or ran local
pruning.

Release evidence must therefore include, from the target pilot environment:

1. the erasure manifest and live canary absence;
2. successful private-object deletion outbox settlement;
3. calendar/specialist-provider deletion receipts where applicable;
4. the installed bucket lifecycle policy and an expiry/deletion event or object
   inventory proving no archive older than 35 days; and
5. local archive prune output plus a restore inventory showing that the expired
   pre-erasure backup is no longer available.

Until those infrastructure/provider artifacts exist, the accurate claim is:
live application stores are erased and backup expiry is scheduled, not that all
backup or downstream-provider bytes have already been physically destroyed.

## Deterministic regression evidence

- `tests/test_privacy_erasure_graph.py` plants one unique canary in candidate,
  contact, encrypted job, cursor, inbound/outbound webhook, notification,
  audit, communication batch, usage ledger, and queue lease stores. It proves
  deletion/pseudonymization, accounting preservation, and a 35-day manifest
  containing neither the candidate UUID nor canary.
- The same suite encrypts a candidate webhook with an unavailable key and
  proves HTTP 409, candidate preservation, payload preservation, and no false
  expiry manifest.
- `tests/test_retention_private_objects.py` proves registered, derived, and
  legacy byte deletion, legal-hold closure, and resource-ID pseudonymization.
- `tests/test_ai_candidate_source_erasure.py` proves AI attachment provenance,
  legal-hold behavior, deletion outbox creation, and physical worker deletion.
- `tests/test_domain_provider_wiring.py` proves encrypted calendar deletion
  survives the candidate cascade, retry retains runnable ciphertext, terminal
  success clears it, and ambiguous provider create fails closed.
- `tests/test_provider_assessment_domain.py` proves the first retention pass
  retains the candidate while provider deletion is pending, the simulator
  confirms deletion through a durable worker, terminal ciphertext is cleared,
  and only a later retention pass admits the candidate to live erasure.
- `tests/test_integration_provider_contracts.py` proves the specialist erasure
  endpoint, idempotency header, bounded request, confirmed receipt contract, and
  receipt hashing without retaining the raw receipt.
