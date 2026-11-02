# Authorization and PostgreSQL RLS Requirements

This document is the schema and rollout contract for the authorization layer.
Application code already binds a verified tenant identity to each SQLAlchemy
session, reapplies transaction-local PostgreSQL settings after commit, rejects
cross-tenant ORM writes, applies tenant/assignment loader criteria, and denies
unclassified HTTP operations. RLS is the final database boundary and must be
introduced by the schema steward in the single sequential migration chain.

## Identity and session contract

Only a successful database identity lookup may establish tenant context. Never
accept an organization identifier from a JWT claim, header, query parameter,
request body, webhook payload, or worker payload as authority.

The application sets these values with transaction-local
`set_config(..., true)` and reapplies them whenever the same logical session
begins a new transaction:

| Setting | Source |
|---|---|
| `app.organization_id` | organization on the verified user/API key/capability/claimed work row |
| `app.actor_id` | verified user/API-key ID, or empty for a public/background identity |
| `app.identity_kind` | allowlisted `user`, `api_key`, `public`, `background`, or `system` |
| `app.actor_role` | verified role, or empty for a non-user identity |

The runtime database role must be `NOSUPERUSER NOBYPASSRLS` and must not own the
protected tables. Connection-pool reset remains mandatory. Transaction-local
settings prevent one returned connection from carrying another request's
tenant into its next checkout.

## Authentication bootstrap functions

Forced RLS makes an ordinary pre-context lookup impossible. Create narrowly
scoped `SECURITY DEFINER` functions owned by a non-login broker role. Every
function must:

- declare `SET search_path = pg_catalog` and fully qualify all schema/object names;
- accept no organization ID;
- return only the identifiers and authentication columns needed to establish a
  context, never profile/tenant payloads;
- reject inactive, expired, revoked, or unusable identities in the function;
- be revoked from `PUBLIC` and granted only to the specific application role;
- avoid dynamic SQL and avoid caller-controlled relation, column, sort, or
  predicate fragments;
- have regression tests proving spoofed identifiers cannot select another tenant.

Required primitives are conceptually:

```sql
security.claim_user_by_id(p_user_id uuid)
  returns (user_id uuid, organization_id uuid, password/token/MFA/status columns...)

security.claim_user_by_email(p_email citext)
  returns (user_id uuid, organization_id uuid, password/token/MFA/status columns...)

security.claim_api_key(p_digest bytea, p_now timestamptz)
  returns (api_key_id uuid, organization_id uuid, created_by_user_id uuid, scopes text[])

security.claim_refresh_token(p_digest bytea, p_now timestamptz)
  returns (token_id uuid, session_id uuid, user_id uuid, organization_id uuid)

security.claim_public_capability(p_kind security.capability_kind, p_digest bytea,
                                 p_now timestamptz)
  returns (capability_id uuid, organization_id uuid, resource_id uuid)
```

The refresh claim only locates an otherwise valid candidate generation; after
binding its returned organization, normal tenant-scoped code locks and validates
the token, family, and user before rotation. Use one allowlisted capability kind
(or separate functions) for assessment,
shortlist, scheduling/manage, offer, referral, careers-apply, and inbound webhook
tokens. A generic function must not accept an arbitrary table name. After a
function returns, application code binds its returned organization and performs
all payload reads/mutations in a normal tenant-bound transaction.

## Durable global worker claims

Workers must discover globally claimable rows without `BYPASSRLS` and without
temporarily disabling RLS. Use one fixed-search-path `SECURITY DEFINER` claim
function per queue. The functions atomically select due rows with
`FOR UPDATE SKIP LOCKED`, set a bounded lease, and return only work identity:

```sql
security.claim_integration_jobs(p_worker_id text, p_limit integer, p_now timestamptz)
security.claim_webhook_deliveries(p_worker_id text, p_limit integer, p_now timestamptz)
security.claim_communication_messages(p_worker_id text, p_limit integer, p_now timestamptz)
security.claim_interview_reminders(p_worker_id text, p_limit integer, p_now timestamptz)
security.claim_workflow_runs(p_worker_id text, p_limit integer, p_now timestamptz)
security.claim_mission_steps(p_worker_id text, p_limit integer, p_now timestamptz)

-- each returns only:
(work_id uuid, organization_id uuid, lease_token uuid, lease_expires_at timestamptz)
```

Each function validates a bounded limit, uses fixed queue predicates, and never
returns payload, PII, credentials, provider tokens, or LLM context. `PUBLIC` has
no execute privilege; only a dedicated non-login-owner worker role does. For
each returned row the worker opens a fresh `tenant_session_scope` using the
returned organization, then fetches by `(organization_id, work_id,
lease_token/owner)` before processing. Completion, retry, and dead-letter
updates must match the same tenant and active lease. A worker may not process
several organizations in one ORM session. Lease expiry permits crash recovery;
a stale worker cannot acknowledge a re-leased row.

Worker heartbeats contain no tenant data and belong in a separate global table
with a narrowly granted upsert function. Retention/maintenance must claim one
organization ID through a similarly fixed function, then run all deletion in a
tenant-bound session.

## Tenant tables and policy

The current model metadata contains 92 relations with `organization_id`; every
one requires `ENABLE ROW LEVEL SECURITY`, `FORCE ROW LEVEL SECURITY`, and an
organization policy using the same null-safe predicate for both read and write:

```sql
USING (
  organization_id = nullif(current_setting('app.organization_id', true), '')::uuid
)
WITH CHECK (
  organization_id = nullif(current_setting('app.organization_id', true), '')::uuid
)
```

Those relations are:

```text
ai_action_bundles
ai_agent_runs
ai_messages
ai_organization_settings
ai_sessions
application_stage_events
applications
assessment_answers
assessment_attempts
assessment_files
assessment_invitations
assessment_questions
assessment_results
assessment_sections
assessment_template_versions
assessment_templates
audit_logs
availability_blocks
availability_rules
candidate_activities
candidate_contact_points
candidate_handoffs
candidate_notes
candidate_offers
candidate_saved_views
candidate_tags
candidates
chat_sessions
communication_accounts
communication_batches
communication_messages
communication_templates
communication_threads
enterprise_policies
evidence_claims
folders
hiring_decision_approvals
hiring_decisions
hiring_missions
integration_connections
integration_cursors
integration_jobs
integration_oauth_states
integration_webhook_deliveries
integration_webhook_delivery_attempts
integration_webhook_events
interview_appointment_interviewers
interview_appointments
interview_questions
interview_recordings
interview_reminders
interview_scheduling_link_interviewers
interview_scheduling_links
interviews
messages
mission_candidate_assessments
mission_events
mission_plan_versions
mission_step_attempts
mission_steps
notifications
offer_approvals
offer_templates
organization_api_keys
organization_entitlements
organization_model_configurations
permission_overrides
positions
referral_links
requisition_approvals
requisitions
scorecard_assignments
scorecard_criteria
scorecard_ratings
scorecard_templates
scores
shortlist_links
source_candidate_receipts
source_import_runs
source_integrations
source_publications
sso_configurations
talent_nurture_enrollments
talent_nurture_sequences
talent_nurture_steps
talent_pool_members
talent_pools
task_submissions
usage_ledger_entries
users
workflow_rules
workflow_runs
```

Four existing security relations are indirectly tenant-owned and must acquire a
non-null `organization_id` in the consolidated migration: `revoked_access_tokens`,
`auth_refresh_sessions`, `auth_refresh_tokens`, and
`user_mfa_recovery_codes`. Backfill the first, second, and fourth through
`users`; backfill refresh tokens through their refresh session. Add them to the
forced-RLS inventory, bringing the post-migration protected relation count to
96. Add `UNIQUE (organization_id, id)` and these composite foreign keys:

- `revoked_access_tokens (organization_id, user_id)` to `users`;
- `auth_refresh_sessions (organization_id, user_id)` to `users`;
- `user_mfa_recovery_codes (organization_id, user_id)` to `users`;
- `auth_refresh_tokens (organization_id, session_id)` to
  `auth_refresh_sessions`;
- `auth_refresh_tokens (organization_id, replaced_by_token_id)` to
  `auth_refresh_tokens` (nullable, `ON DELETE SET NULL`).

Keep the existing keyed token/code digest uniqueness constraints global. An
identifier collision across tenants must never create two valid security
credentials. `auth_login_throttles` is the deliberate global exception: it
contains only keyed account/network digests and counters, has bounded cleanup,
and is used before a tenant identity is known. `worker_heartbeats` is also
global and non-sensitive. Every other table without `organization_id` must be
demonstrably global/non-sensitive or acquire tenant ownership. Indirectly owned
children must not rely on application joins as their only boundary.

## Keys, assignments, and constraints

Add `UNIQUE (organization_id, id)` to every tenant parent and convert tenant
relations to composite foreign keys `(organization_id, parent_id)` referencing
the corresponding parent. This prevents a valid tenant ID combined with a
foreign parent ID from forming a cross-tenant nested relation even if application
validation is bypassed. Add tenant-leading indexes for every composite FK and
high-volume scoped lookup.

At minimum, cover all candidate/position/application/assessment/interview,
scorecard, requisition/offer, communication, scheduling, workflow, sourcing,
AI, and integration edges represented by the table inventory above. Migration
validation must fail if a tenant child foreign key remains ID-only.

Exact interviewer scoping also requires a first-class assignment relation;
candidate/position scorecard assignment is not enough to distinguish two
interviews for the same pair:

```text
interview_assignments
  id uuid primary key
  organization_id uuid not null
  interview_id uuid not null
  interviewer_user_id uuid not null
  assigned_by_user_id uuid not null
  created_at timestamptz not null

  unique (organization_id, interview_id, interviewer_user_id)
  fk (organization_id, interview_id) -> interviews (organization_id, id)
  fk (organization_id, interviewer_user_id) -> users (organization_id, id)
  fk (organization_id, assigned_by_user_id) -> users (organization_id, id)
  index (organization_id, interviewer_user_id, interview_id)
```

The `users.role` constraint and all role validators must accept exactly
`admin`, `recruiter`, `hiring_manager`, `interviewer`, and `restricted`.
`permission_overrides` must be unique on `(organization_id, role, permission)`;
role values are constrained to the same set, while permission values remain
application-validated so canonical IDs can evolve without an unsafe migration
race. Update `enterprise_policies.field_visibility` database defaults to include
the canonical field keys and role arrays from `app.auth.authorization`.

## Rollout gate and verification

Do not enable forced RLS until every identity bootstrap and every public-token,
API-key, worker, webhook, maintenance, and background path uses the primitives
above. Enabling it earlier would fail closed but would break those workflows;
leaving it partially enabled would create a misleading security claim.

The migration is release-ready only when PostgreSQL integration tests prove:

1. no context, empty context, malformed UUID context, and a spoofed tenant all
   return zero protected rows and cannot insert/update/delete;
2. pooled connection reuse and a mid-handler commit cannot retain the previous
   tenant, actor, or role;
3. user/API-key/capability bootstrap functions return only their database-bound
   tenant and reject inactive, revoked, expired, and invalid credentials;
4. every worker claim handles concurrency with no duplicate delivery, returns
   no payload/PII, binds a fresh tenant session, and rejects stale lease updates;
5. cross-tenant IDs in path, query, body, nested relation, bulk lists, imports,
   exports, attachments, AI context, backups, and audit queries yield generic
   not-found/denied responses, perform no mutation, and disclose no row counts;
6. hiring managers see only requisition-linked positions and candidates, and
   interviewers see only explicit interview/scorecard assignments;
7. action grants cannot bypass field policy, field grants cannot bypass action
   permission, and an exact deny overrides role defaults/legacy aliases;
8. metadata inspection finds no `organization_id` table without forced RLS and
   no tenant relation lacking a composite foreign key.

Until these gates pass, PostgreSQL RLS is a documented migration requirement,
not a completed production control. Application-level ORM scoping is defense in
depth and does not protect raw SQL.
