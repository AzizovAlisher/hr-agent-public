# Permission and Data Policy

FastAPI enforcement is authoritative and fail-closed. Permission overrides are
evaluated on every request and must affect both the API and navigation. Resource
assignment checks are an additional constraint; a broad action permission does
not imply access to an unrelated requisition, interview, or application.

## Canonical action IDs

| Domain | Read | Mutate/admin | Sensitive or external action |
|---|---|---|---|
| account | `account.read` | `account.manage` | - |
| candidates | `candidates.read` | `candidates.write` | `candidates.contact`, `candidates.documents`, `candidates.export` |
| applications | `applications.read` | `applications.write` | `applications.disposition` |
| assessments | `assessments.read` | `assessments.manage` | `assessments.grade`, `assessments.results` |
| interviews/scorecards | `interviews.read` | `interviews.manage` | `scorecards.submit` |
| requisitions | `requisitions.read` | `requisitions.manage` | `requisitions.approve`, `requisitions.compensation` |
| offers | `offers.read` | `offers.manage` | `offers.compensation` |
| communications | `communications.read` | `communications.manage` | `communications.send` |
| scheduling | `scheduling.read` | `scheduling.manage` | - |
| workflows | `workflows.read` | `workflows.manage` | `workflows.execute` |
| analytics | `analytics.read` | - | `analytics.export` |
| AI | `ai.use` | `ai.manage` | `ai.managed_provider`, `ai.sensitive_context` |
| integrations | `integrations.read` | `integrations.manage` | `integrations.credentials` |
| sourcing | `sourcing.read` | `sourcing.manage` | `sourcing.publish` |
| talent | `talent.read` | `talent.manage` | - |
| enterprise | `enterprise.read` | `enterprise.manage` | `enterprise.audit_export`, `enterprise.backup_export` |

Existing coarse action IDs remain compatibility aliases until every route and
generated client has moved to this vocabulary. New endpoints must use canonical
IDs and must not introduce resource-specific substitutes for unrelated actions.

## Default role policy

| Role | Default resource scope | Allowed behavior |
|---|---|---|
| admin | organization | all actions; cannot remove or demote the last active admin |
| recruiter | organization hiring workflow | candidate/application and position work, including job-text compensation context; interviews, offers, and communications; no enterprise secrets |
| hiring manager | assigned requisitions/positions | read assigned candidates and job-text compensation context; approve/score/interview as assigned |
| interviewer | assigned interviews | redacted candidate context and assigned scorecards only; blind feedback until submission/lock |
| restricted | explicit grants | no action unless enabled by a tested override |
| API key | declared scopes | non-interactive endpoint scopes only; expiry/revocation required |

## Field classes

| Class | Examples | Minimum policy |
|---|---|---|
| public | published job title, public branding | intentionally published, cache policy documented |
| internal | stage, owner, workflow status | authenticated tenant member plus action permission |
| candidate contacts | email, phone, Telegram handle, address | `candidates.contact` plus field-role policy; redact in exports/AI by default |
| candidate CV | CV, work sample, parsed text | `candidates.documents` plus field-role policy; private download and sensitive-read audit |
| assessment results | answers, grades, evidence, scorecards | `assessments.results` plus field-role policy and assignment; blind-feedback rules |
| compensation | requisition range, job-description pay context, offer terms | `requisitions.compensation` for requisition/position context, `offers.compensation` for offer terms; never included in broad exports |
| offers | offer existence/status/terms | offer permission plus field-role policy |
| exports | bulk candidate/analytics data | export action plus field-role policy and explicit contact opt-in |
| backups | tenant backup archives | `enterprise.backup_export` plus admin-only field policy |
| AI context | candidate/job material sent to a model | `ai.sensitive_context` plus field-role policy; no implicit CV/contact access |
| secret | OAuth tokens, API keys, MFA seeds/recovery | encrypted at rest; never returned after creation/rotation |
| regulated/audit | consent, erasure/legal hold, security log | admin-limited, append-only where applicable |

Every response schema and export must declare the field classes it exposes.
Server-side redaction occurs before serialization; the frontend must never receive
hidden fields.

For the private-pilot compatibility release, recruiters and hiring managers have
`requisitions.compensation` and the compensation field class by default so their
normal position workflows remain usable. Tenant overrides may deny either one;
an explicit deny remains authoritative and then position text is redacted and
unstructured position writes fail closed. The historical expansion from
`requisitions.compensation` to `offers.compensation` remains for one release,
but an explicit canonical `offers.compensation` override always wins.

Position create and project-create are wholly unstructured and therefore declare
the compensation action and field in the operation catalog. Position PATCH is a
mixed-field adapter: the catalog declares its base `candidates.write` action,
while the handler additionally requires `requisitions.compensation` and the
compensation field class whenever title, description, requirements, internal
notes, folder text, candidate profile, or public description is present. Purely
structural PATCH fields remain available without compensation access. Position
reads use structural server-side redaction before serialization rather than
requiring the compensation field for the entire operation.

Assigned interviewers are application-cycle scoped. They may read their
assigned `Interview`, `ScorecardAssignment`, the corresponding `Application`,
and application-linked native assessment records. Legacy task submissions,
recordings, communications, chat sessions, workflow runs, source receipts,
processing jobs, activities, and AI mission assessments do not carry an
authoritative `application_id`; those surfaces are unavailable to interviewers
even when the candidate and position happen to match. Scheduling links and
appointments are the narrow exception only when their non-null `interview_id`
points to the explicitly assigned interview. Candidate-global CRM artifacts
(notes, tags, contacts, saved candidate state, talent membership, and nurture
enrollment) are neither readable nor mutable through an assigned application.
Hiring managers retain exact candidate-position access for positions they own,
but nullable or independently crossed legacy relationships remain hidden.

Legacy task/recording evidence may enter scoring, interview generation, or chat
only for the requested position when exactly one application cycle exists for
that candidate-position pair. Reapplication makes those unparented artifacts
ambiguous, so they fail closed until migrated to an authoritative Application
parent.

## Enforcement and review contract

`app.auth.authorization.OPERATION_CATALOG` is the reviewed, exact
`(HTTP method, OpenAPI path template)` allowlist. It classifies authentication,
action IDs, assignment scope, and sensitive field classes for every operation.
There is no prefix or default policy. A new or changed operation must fail the
catalog parity test until it is reviewed. The current candidate catalog
contains 360 operations: 317 bearer, 22 capability-token, 10 public,
6 auth-flow, 3 webhook-secret, and 2 API-key operations.

`GET /health/ready` is public but exposes only status and check time.
`GET /enterprise/operations/readiness` exposes dependency/component diagnostics
and requires the bearer action `enterprise.manage`; the public route must never
be treated as authorization for the diagnostic payload.

The server evaluates action permission and field policy independently: both
must allow access. Exact canonical overrides are applied after compatibility
aliases and therefore are authoritative. Unknown roles, action IDs, field keys,
and unclassified routes fail closed. UI visibility is advisory and must mirror,
not replace, this server policy.

Tenant and assignment scope is bound from a verified database identity, never
from an organization ID in a JWT, header, query string, or request body. The
database/RLS rollout and durable-worker bootstrap contract are specified in
`AUTHORIZATION_AND_RLS_REQUIREMENTS.md`.
