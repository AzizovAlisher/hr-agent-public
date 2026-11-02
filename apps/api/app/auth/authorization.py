"""Canonical authorization vocabulary and reviewed HTTP operation catalog.

The catalog is intentionally an exact method/path allowlist.  There is no
catch-all prefix rule: a new route remains unclassified (and therefore cannot
be served through ``current_user``) until its permission, resource scope, and
sensitive field classes have been reviewed.
"""

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType


class AuthMode(StrEnum):
    PUBLIC = "public"
    AUTH_FLOW = "auth_flow"
    BEARER = "bearer"
    API_KEY = "api_key"
    WEBHOOK_SECRET = "webhook_secret"
    CAPABILITY_TOKEN = "capability_token"


class ResourceScope(StrEnum):
    NONE = "none"
    SELF = "self"
    ORGANIZATION = "organization"
    HIRING_ASSIGNMENT = "hiring_assignment"


class FieldClass(StrEnum):
    INTERNAL = "internal"
    CONTACT = "candidate_contacts"
    CANDIDATE_DOCUMENT = "candidate_cv"
    ASSESSMENT_RESULT = "assessment_results"
    COMPENSATION = "compensation"
    OFFER = "offers"
    EXPORT = "exports"
    BACKUP = "backups"
    AI_CONTEXT = "ai_context"
    SECRET = "secrets"
    AUDIT = "audit"


@dataclass(frozen=True, slots=True)
class OperationPolicy:
    auth_mode: AuthMode
    actions: tuple[str, ...] = ()
    scope: ResourceScope = ResourceScope.NONE
    fields: frozenset[FieldClass] = frozenset()


CANONICAL_PERMISSIONS = frozenset(
    {
        "account.read",
        "account.manage",
        "candidates.read",
        "candidates.write",
        "candidates.contact",
        "candidates.documents",
        "candidates.export",
        "applications.read",
        "applications.write",
        "applications.disposition",
        "assessments.read",
        "assessments.manage",
        "assessments.grade",
        "assessments.results",
        "interviews.read",
        "interviews.manage",
        "scorecards.submit",
        "requisitions.read",
        "requisitions.manage",
        "requisitions.approve",
        "requisitions.compensation",
        "offers.read",
        "offers.manage",
        "offers.compensation",
        "communications.read",
        "communications.manage",
        "communications.send",
        "scheduling.read",
        "scheduling.manage",
        "workflows.read",
        "workflows.manage",
        "workflows.execute",
        "analytics.read",
        "analytics.export",
        "ai.use",
        "ai.manage",
        "ai.managed_provider",
        "ai.sensitive_context",
        "integrations.read",
        "integrations.manage",
        "integrations.credentials",
        "sourcing.read",
        "sourcing.manage",
        "sourcing.publish",
        "talent.read",
        "talent.manage",
        "enterprise.read",
        "enterprise.manage",
        "enterprise.audit_export",
        "enterprise.backup_export",
    }
)

# A compatibility override applies to every canonical action that the legacy
# coarse action used to gate.  Explicit canonical overrides are processed
# afterwards and therefore win deterministically when both forms exist.
PERMISSION_ALIASES: dict[str, tuple[str, ...]] = {
    "scorecards.manage": (
        "assessments.read",
        "assessments.manage",
        "assessments.grade",
        "assessments.results",
        "interviews.manage",
        "scorecards.submit",
    ),
}

# Historical offer endpoints used the requisition compensation toggle.  Keep
# that override effective for offers until clients have adopted the dedicated
# canonical action; an explicit ``offers.compensation`` row wins.
COMPATIBILITY_EXPANSIONS: dict[str, tuple[str, ...]] = {
    "requisitions.compensation": ("offers.compensation",),
}

PERMISSIONS = frozenset(CANONICAL_PERMISSIONS | PERMISSION_ALIASES.keys())
ROLES = frozenset({"admin", "recruiter", "hiring_manager", "interviewer", "restricted"})


ROLE_DEFAULTS: dict[str, frozenset[str]] = {
    "admin": CANONICAL_PERMISSIONS,
    "recruiter": frozenset(
        {
            "account.read",
            "account.manage",
            "candidates.read",
            "candidates.write",
            "candidates.contact",
            "candidates.documents",
            "candidates.export",
            "applications.read",
            "applications.write",
            "applications.disposition",
            "assessments.read",
            "assessments.manage",
            "assessments.grade",
            "assessments.results",
            "interviews.read",
            "interviews.manage",
            "scorecards.submit",
            "requisitions.read",
            "requisitions.compensation",
            "offers.read",
            "offers.manage",
            "communications.read",
            "communications.manage",
            "communications.send",
            "scheduling.read",
            "scheduling.manage",
            "workflows.read",
            "analytics.read",
            "analytics.export",
            "ai.use",
            "ai.sensitive_context",
            "integrations.read",
            "sourcing.read",
            "sourcing.manage",
            "sourcing.publish",
            "talent.read",
            "talent.manage",
            "enterprise.read",
        }
    ),
    "hiring_manager": frozenset(
        {
            "account.read",
            "account.manage",
            "candidates.read",
            "applications.read",
            "applications.disposition",
            "assessments.read",
            "assessments.grade",
            "assessments.results",
            "interviews.read",
            "interviews.manage",
            "scorecards.submit",
            "requisitions.read",
            "requisitions.approve",
            "requisitions.compensation",
            "offers.read",
            "offers.manage",
            "communications.read",
            "scheduling.read",
            "scheduling.manage",
            "workflows.read",
            "analytics.read",
            "ai.use",
            "ai.sensitive_context",
            "enterprise.read",
        }
    ),
    "interviewer": frozenset(
        {
            "account.read",
            "candidates.read",
            "applications.read",
            "assessments.read",
            "assessments.results",
            "interviews.read",
            "scorecards.submit",
            "scheduling.read",
            "enterprise.read",
        }
    ),
    "restricted": frozenset(),
}


FIELD_DEFAULT_ROLES: dict[FieldClass, frozenset[str]] = {
    FieldClass.INTERNAL: frozenset(
        {"admin", "recruiter", "hiring_manager", "interviewer"}
    ),
    FieldClass.CONTACT: frozenset({"admin", "recruiter"}),
    FieldClass.CANDIDATE_DOCUMENT: frozenset({"admin", "recruiter"}),
    FieldClass.ASSESSMENT_RESULT: frozenset(
        {"admin", "recruiter", "hiring_manager", "interviewer"}
    ),
    FieldClass.COMPENSATION: frozenset({"admin", "recruiter", "hiring_manager"}),
    FieldClass.OFFER: frozenset({"admin", "recruiter", "hiring_manager"}),
    FieldClass.EXPORT: frozenset({"admin", "recruiter"}),
    FieldClass.BACKUP: frozenset({"admin"}),
    FieldClass.AI_CONTEXT: frozenset({"admin", "recruiter"}),
    FieldClass.SECRET: frozenset({"admin"}),
    FieldClass.AUDIT: frozenset({"admin"}),
}


_catalog: dict[tuple[str, str], OperationPolicy] = {}


def _register(
    methods: str | tuple[str, ...],
    paths: tuple[str, ...],
    *,
    auth_mode: AuthMode = AuthMode.BEARER,
    actions: tuple[str, ...] = (),
    scope: ResourceScope = ResourceScope.ORGANIZATION,
    fields: tuple[FieldClass, ...] = (),
) -> None:
    method_values = (methods,) if isinstance(methods, str) else methods
    unknown = set(actions) - CANONICAL_PERMISSIONS
    if unknown:
        raise RuntimeError(f"Catalog contains unknown actions: {sorted(unknown)}")
    policy = OperationPolicy(auth_mode, actions, scope, frozenset(fields))
    for method in method_values:
        for path in paths:
            key = (method.upper(), path)
            if key in _catalog:
                raise RuntimeError(f"Duplicate operation policy: {key}")
            _catalog[key] = policy


# Public infrastructure and authentication ceremonies.
_register(
    "GET", ("/health", "/health/live", "/health/ready"), auth_mode=AuthMode.PUBLIC
)
_register(
    "GET",
    ("/candidates/{candidate_id}/processing-jobs/{job_id}",),
    actions=("candidates.write", "ai.use"),
    scope=ResourceScope.HIRING_ASSIGNMENT,
)
_register(
    "POST",
    (
        "/auth/login",
        "/auth/mfa/setup",
        "/auth/mfa/confirm",
        "/auth/mfa/verify",
        "/auth/refresh",
        "/auth/password-reset/request",
    ),
    auth_mode=AuthMode.AUTH_FLOW,
)
_register(
    ("GET", "POST"),
    (
        "/auth/invitations/{token}",
        "/auth/invitations/{token}/accept",
        "/auth/password-reset/{token}",
    ),
    auth_mode=AuthMode.CAPABILITY_TOKEN,
)
_register(
    ("GET", "POST", "DELETE"),
    (
        "/auth/me",
        "/auth/sessions",
        "/auth/sessions/{session_id}",
        "/auth/logout",
        "/auth/logout-all",
        "/auth/mfa/disable",
    ),
    auth_mode=AuthMode.BEARER,
    scope=ResourceScope.SELF,
)

# Token-bearing public surfaces are classified separately from anonymous
# content because their random URL token is the authorization boundary.
_register(
    ("GET", "POST", "PATCH", "DELETE"),
    (
        "/public/assessments/{token}",
        "/public/assessments/{token}/attempts",
        "/public/assessments/{token}/attempts/{attempt_id}/answers",
        "/public/assessments/{token}/attempts/{attempt_id}/files/{file_id}",
        "/public/assessments/{token}/attempts/{attempt_id}/questions/{question_id}/files",
        "/public/assessments/{token}/attempts/{attempt_id}/submit",
        "/public/offers/{token}",
        "/public/offers/{token}/respond",
        "/public/referrals/{token}",
        "/public/scheduling/manage/{manage_token}",
        "/public/scheduling/manage/{manage_token}/cancel",
        "/public/scheduling/manage/{manage_token}/reschedule",
        "/public/scheduling/manage/{manage_token}/slots",
        "/public/scheduling/{token}",
        "/public/scheduling/{token}/book",
        "/public/scheduling/{token}/slots",
        "/public/shortlists/{token}",
    ),
    auth_mode=AuthMode.CAPABILITY_TOKEN,
)
_register(
    ("GET", "POST"),
    (
        "/public/positions/{position_id}",
        "/public/positions/{position_id}/apply",
        "/public/v1/careers/widget.js",
        "/public/v1/careers/{organization_slug}/positions",
        "/public/v1/careers/{organization_slug}/positions/{position_id}",
        "/public/v1/careers/{organization_slug}/positions/{position_id}/apply",
        "/public/v1/careers/{organization_slug}/preview",
    ),
    auth_mode=AuthMode.PUBLIC,
)
_register(
    "POST",
    (
        "/communications/webhooks/sandbox/{account_id}",
        "/source-webhooks/{integration_id}",
    ),
    auth_mode=AuthMode.WEBHOOK_SECRET,
)
_register(
    "GET",
    ("/enterprise/api/v1/candidates", "/enterprise/api/v1/positions"),
    auth_mode=AuthMode.API_KEY,
)

# Candidate core, documents, contact data, and exports.
_register(
    "GET",
    (
        "/candidates",
        "/candidates/{candidate_id}",
        "/candidates/{candidate_id}/applications",
        "/candidates/{candidate_id}/activities",
        "/candidates/{candidate_id}/merged-sources",
        "/candidates/{candidate_id}/notes",
        "/candidates/{candidate_id}/tags",
        "/candidate-search",
        "/candidate-duplicates",
        "/candidate-views",
    ),
    actions=("candidates.read",),
    scope=ResourceScope.HIRING_ASSIGNMENT,
)
_register(
    "GET",
    ("/candidates/{candidate_id}/best-positions",),
    actions=("candidates.read",),
    scope=ResourceScope.HIRING_ASSIGNMENT,
    fields=(FieldClass.ASSESSMENT_RESULT,),
)
_register(
    "GET",
    ("/candidates/{candidate_id}/merged-sources/{source_candidate_id}/evidence",),
    actions=("candidates.read", "candidates.documents", "candidates.contact"),
    scope=ResourceScope.HIRING_ASSIGNMENT,
    fields=(FieldClass.CANDIDATE_DOCUMENT, FieldClass.CONTACT),
)
_register(
    "GET",
    (
        "/candidates/{candidate_id}/cv",
        "/candidates/{candidate_id}/merged-sources/{source_candidate_id}/cv",
    ),
    actions=("candidates.read", "candidates.documents", "candidates.contact"),
    scope=ResourceScope.HIRING_ASSIGNMENT,
    fields=(FieldClass.CANDIDATE_DOCUMENT, FieldClass.CONTACT),
)
_register(
    ("POST", "PATCH", "DELETE", "PUT"),
    (
        "/candidates/upload/file",
        "/candidates/upload/hh",
        "/candidates/upload/text",
        "/candidates/{candidate_id}",
        "/candidates/{candidate_id}/notes",
        "/candidates/{candidate_id}/notes/{note_id}",
        "/candidates/{candidate_id}/tags",
        "/candidates/{candidate_id}/tags/{tag_id}",
        "/candidate-bulk",
        "/candidate-views",
        "/candidate-views/{view_id}",
    ),
    actions=("candidates.write",),
    scope=ResourceScope.HIRING_ASSIGNMENT,
)
_register(
    "POST",
    (
        "/candidates/{primary_candidate_id}/merge",
        "/candidate-merges/preview",
    ),
    actions=("candidates.write",),
    scope=ResourceScope.HIRING_ASSIGNMENT,
    fields=(FieldClass.ASSESSMENT_RESULT,),
)
_register(
    "GET",
    ("/candidates/{candidate_id}/merge-history",),
    actions=("candidates.write",),
    scope=ResourceScope.ORGANIZATION,
)
_register(
    "POST",
    ("/candidate-merges/{batch_id}/reverse",),
    actions=("candidates.write",),
    scope=ResourceScope.ORGANIZATION,
)
_register(
    "POST",
    ("/candidates/{candidate_id}/view",),
    actions=("candidates.read",),
    scope=ResourceScope.HIRING_ASSIGNMENT,
)
_register(
    "GET",
    ("/candidates/{candidate_id}/github/detect",),
    actions=("candidates.read",),
    scope=ResourceScope.HIRING_ASSIGNMENT,
    fields=(FieldClass.CONTACT, FieldClass.CANDIDATE_DOCUMENT),
)
_register(
    ("PUT", "DELETE"),
    ("/candidates/{candidate_id}/github",),
    actions=("candidates.write",),
    scope=ResourceScope.HIRING_ASSIGNMENT,
    fields=(FieldClass.CONTACT, FieldClass.CANDIDATE_DOCUMENT),
)
_register(
    "POST",
    ("/candidate-export",),
    actions=("candidates.export",),
    scope=ResourceScope.HIRING_ASSIGNMENT,
    fields=(FieldClass.EXPORT,),
)

# Candidate contact points and communication content.
_register(
    "GET",
    ("/candidates/{candidate_id}/contact-points",),
    actions=("candidates.read", "candidates.contact"),
    scope=ResourceScope.HIRING_ASSIGNMENT,
    fields=(FieldClass.CONTACT,),
)
_register(
    ("POST", "PATCH"),
    (
        "/candidates/{candidate_id}/contact-points",
        "/candidates/{candidate_id}/contact-points/{contact_point_id}",
    ),
    actions=("candidates.write", "candidates.contact"),
    scope=ResourceScope.HIRING_ASSIGNMENT,
    fields=(FieldClass.CONTACT,),
)
_register(
    "GET",
    (
        "/candidates/{candidate_id}/communication-threads",
        "/communications/threads",
        "/communications/threads/{thread_id}/messages",
        "/communications/templates",
    ),
    actions=("communications.read",),
    scope=ResourceScope.HIRING_ASSIGNMENT,
    fields=(FieldClass.CONTACT,),
)
_register(
    "POST",
    (
        "/candidates/{candidate_id}/communications/preview",
        "/communications/bulk/preview",
    ),
    actions=("communications.read", "candidates.contact"),
    scope=ResourceScope.HIRING_ASSIGNMENT,
    fields=(FieldClass.CONTACT,),
)
_register(
    "POST",
    (
        "/candidates/{candidate_id}/communications/send",
        "/communications/bulk",
    ),
    actions=("communications.send", "candidates.contact"),
    scope=ResourceScope.HIRING_ASSIGNMENT,
    fields=(FieldClass.CONTACT,),
)
_register(
    ("POST", "PATCH"),
    (
        "/communications/templates",
        "/communications/templates/{template_id}",
        "/communications/threads/{thread_id}/read",
        "/communications/messages/{message_id}/cancel",
    ),
    actions=("communications.manage",),
)
_register(
    "GET",
    ("/communications/accounts",),
    # List responses contain sender identity and provider capability state but
    # never OAuth/API credentials; those remain in the encrypted integration
    # credential store and are exposed only by one-time create/rotate flows.
    # Recruiters need this safe projection to select a real delivery account.
    actions=("communications.read",),
)
_register(
    "GET",
    ("/communications/capabilities",),
    actions=("communications.read",),
)
_register(
    "POST",
    (
        "/communications/accounts",
        "/communications/accounts/{account_id}/rotate-webhook-secret",
    ),
    actions=("integrations.manage", "integrations.credentials"),
    fields=(FieldClass.SECRET,),
)

# Positions and hiring pipeline operations.
_register(
    "GET",
    (
        "/positions",
        "/positions/{position_id}",
        "/positions/{position_id}/applications",
    ),
    actions=("candidates.read",),
    scope=ResourceScope.HIRING_ASSIGNMENT,
)
_register(
    "POST",
    ("/positions", "/positions/project"),
    actions=("candidates.write", "requisitions.compensation"),
    scope=ResourceScope.HIRING_ASSIGNMENT,
    fields=(FieldClass.COMPENSATION,),
)
_register(
    ("POST", "PATCH", "DELETE"),
    ("/positions/move", "/positions/{position_id}"),
    actions=("candidates.write",),
    scope=ResourceScope.HIRING_ASSIGNMENT,
)
_register(
    "POST",
    ("/positions/draft/extract-text",),
    actions=("candidates.write",),
    scope=ResourceScope.HIRING_ASSIGNMENT,
)
_register(
    "POST",
    ("/positions/draft", "/positions/project/extract"),
    actions=(
        "candidates.write",
        "ai.use",
        "ai.sensitive_context",
        "requisitions.compensation",
    ),
    scope=ResourceScope.HIRING_ASSIGNMENT,
    fields=(FieldClass.AI_CONTEXT, FieldClass.COMPENSATION),
)
_register(
    "POST",
    ("/positions/{position_id}/generate-profile", "/positions/{position_id}/rescore"),
    actions=(
        "candidates.write",
        "ai.use",
        "ai.sensitive_context",
        "requisitions.compensation",
    ),
    scope=ResourceScope.HIRING_ASSIGNMENT,
    fields=(FieldClass.AI_CONTEXT, FieldClass.COMPENSATION),
)
_register(
    "GET",
    ("/positions/{position_id}/rescore-capability",),
    actions=(
        "candidates.write",
        "ai.use",
        "ai.sensitive_context",
        "requisitions.compensation",
    ),
    scope=ResourceScope.HIRING_ASSIGNMENT,
    fields=(FieldClass.AI_CONTEXT, FieldClass.COMPENSATION),
)
_register(
    "PATCH",
    ("/positions/{position_id}/candidates/{candidate_id}/stage",),
    actions=("applications.disposition",),
    scope=ResourceScope.HIRING_ASSIGNMENT,
)
_register(
    "PATCH",
    ("/positions/{position_id}/candidates/{candidate_id}/work",),
    actions=("applications.write",),
    scope=ResourceScope.HIRING_ASSIGNMENT,
)

# Applications and native assessments.
_register(
    "GET",
    ("/applications", "/applications/{application_id}"),
    actions=("applications.read",),
    scope=ResourceScope.HIRING_ASSIGNMENT,
)
_register(
    ("POST", "PATCH", "DELETE"),
    ("/applications", "/applications/{application_id}"),
    actions=("applications.write",),
    scope=ResourceScope.HIRING_ASSIGNMENT,
)
_register(
    "POST",
    ("/applications/{application_id}/stage",),
    actions=("applications.disposition",),
    scope=ResourceScope.HIRING_ASSIGNMENT,
)
_register(
    "GET",
    ("/assessment-templates", "/assessment-templates/{template_id}"),
    actions=("assessments.read",),
)
_register(
    ("POST", "PATCH"),
    (
        "/assessment-templates",
        "/assessment-templates/{template_id}",
        "/assessment-templates/{template_id}/versions",
        "/applications/{application_id}/assessment-invitations",
        "/assessment-invitations/{invitation_id}/revoke",
        "/assessment-invitations/{invitation_id}/reissue",
        "/assessment-invitations/{invitation_id}/retakes",
    ),
    actions=("assessments.manage",),
    scope=ResourceScope.HIRING_ASSIGNMENT,
)
_register(
    "GET",
    ("/applications/{application_id}/assessment-invitations",),
    actions=("assessments.read",),
    scope=ResourceScope.HIRING_ASSIGNMENT,
)
_register(
    "GET",
    ("/assessment-invitations/{invitation_id}/attempts",),
    actions=("assessments.results",),
    scope=ResourceScope.HIRING_ASSIGNMENT,
    fields=(FieldClass.ASSESSMENT_RESULT,),
)
_register(
    "GET",
    ("/assessment-attempts/{attempt_id}", "/assessment-files/{file_id}/download"),
    actions=("assessments.results",),
    scope=ResourceScope.HIRING_ASSIGNMENT,
    fields=(FieldClass.ASSESSMENT_RESULT, FieldClass.CANDIDATE_DOCUMENT),
)
_register(
    "POST",
    ("/assessment-attempts/{attempt_id}/manual-grade",),
    actions=("assessments.grade",),
    scope=ResourceScope.HIRING_ASSIGNMENT,
    fields=(FieldClass.ASSESSMENT_RESULT,),
)

# Interviews, scorecards, recordings, tasks, and decisions.
_register(
    "GET",
    (
        "/candidates/{candidate_id}/interviews",
        "/interviews/{interview_id}",
        "/candidates/{candidate_id}/recordings",
        "/recordings/{recording_id}",
        "/candidates/{candidate_id}/tasks",
        "/tasks/{task_id}",
        "/candidates/{candidate_id}/scorecards",
        "/scorecard-templates",
    ),
    actions=("interviews.read",),
    scope=ResourceScope.HIRING_ASSIGNMENT,
    fields=(FieldClass.ASSESSMENT_RESULT,),
)
_register(
    ("POST", "PUT", "DELETE"),
    ("/integrations/{connection_id}/calendar/events",),
    actions=("integrations.manage", "scheduling.manage"),
)
_register(
    "POST",
    (
        "/integrations/{connection_id}/email/messages",
        "/integrations/{connection_id}/telegram/messages",
    ),
    actions=("integrations.manage", "communications.send"),
    fields=(FieldClass.CONTACT,),
)
_register(
    "POST",
    ("/integrations/{connection_id}/hh/imports",),
    actions=("integrations.manage", "sourcing.manage"),
)
_register(
    "POST",
    ("/integrations/{connection_id}/hh/vacancies",),
    actions=("integrations.manage", "sourcing.publish"),
)
_register(
    ("POST", "PATCH", "DELETE"),
    (
        "/candidates/{candidate_id}/interviews/generate",
        "/interviews/{interview_id}",
        "/candidates/{candidate_id}/recordings",
        "/recordings/{recording_id}",
        "/candidates/{candidate_id}/tasks",
        "/tasks/{task_id}",
        "/candidates/{candidate_id}/scorecards/assign",
        "/scorecard-templates",
        "/scorecard-templates/{template_id}",
        "/candidates/{candidate_id}/decisions",
    ),
    actions=("interviews.manage",),
    scope=ResourceScope.HIRING_ASSIGNMENT,
    fields=(FieldClass.ASSESSMENT_RESULT,),
)
_register(
    "POST",
    (
        "/candidates/{candidate_id}/interviews",
        "/interviews/{interview_id}/kit-versions",
    ),
    actions=("interviews.manage",),
    scope=ResourceScope.HIRING_ASSIGNMENT,
    fields=(FieldClass.ASSESSMENT_RESULT,),
)
_register(
    "PATCH",
    ("/interviews/{interview_id}/questions/{question_id}",),
    actions=("scorecards.submit",),
    scope=ResourceScope.HIRING_ASSIGNMENT,
    fields=(FieldClass.ASSESSMENT_RESULT,),
)
_register(
    "POST",
    ("/interviews/{interview_id}/complete",),
    actions=("scorecards.submit",),
    scope=ResourceScope.HIRING_ASSIGNMENT,
    fields=(FieldClass.ASSESSMENT_RESULT,),
)
_register(
    ("PATCH", "POST"),
    (
        "/scorecard-assignments/{assignment_id}",
        "/scorecard-assignments/{assignment_id}/submit",
    ),
    actions=("scorecards.submit",),
    scope=ResourceScope.HIRING_ASSIGNMENT,
    fields=(FieldClass.ASSESSMENT_RESULT,),
)
_register(
    "POST",
    ("/hiring-decisions/{decision_id}/vote",),
    actions=("interviews.manage",),
    scope=ResourceScope.HIRING_ASSIGNMENT,
    fields=(FieldClass.ASSESSMENT_RESULT,),
)
_register(
    "POST",
    ("/tasks/{task_id}/evaluate",),
    actions=("assessments.grade", "ai.use", "ai.sensitive_context"),
    scope=ResourceScope.HIRING_ASSIGNMENT,
    fields=(FieldClass.ASSESSMENT_RESULT, FieldClass.AI_CONTEXT),
)
_register(
    "GET",
    ("/tasks/{task_id}/file",),
    actions=("interviews.read", "candidates.documents", "candidates.contact"),
    scope=ResourceScope.HIRING_ASSIGNMENT,
    fields=(FieldClass.CANDIDATE_DOCUMENT, FieldClass.CONTACT),
)
_register(
    ("POST", "DELETE"),
    ("/tasks/{task_id}/file",),
    actions=("interviews.manage", "candidates.documents", "candidates.contact"),
    scope=ResourceScope.HIRING_ASSIGNMENT,
    fields=(FieldClass.CANDIDATE_DOCUMENT, FieldClass.CONTACT),
)
_register(
    "GET",
    ("/recordings/{recording_id}/file",),
    actions=("interviews.read", "candidates.documents"),
    scope=ResourceScope.HIRING_ASSIGNMENT,
    fields=(FieldClass.ASSESSMENT_RESULT, FieldClass.CANDIDATE_DOCUMENT),
)
_register(
    ("POST", "DELETE"),
    ("/recordings/{recording_id}/file",),
    actions=("interviews.manage", "candidates.documents"),
    scope=ResourceScope.HIRING_ASSIGNMENT,
    fields=(FieldClass.ASSESSMENT_RESULT, FieldClass.CANDIDATE_DOCUMENT),
)

# Requisitions, offers, handoff, and compensation.
_register(
    "GET",
    ("/requisitions",),
    actions=("requisitions.read",),
    scope=ResourceScope.HIRING_ASSIGNMENT,
)
_register(
    ("POST", "PATCH"),
    (
        "/requisitions",
        "/requisitions/{requisition_id}",
        "/requisitions/{requisition_id}/submit",
        "/requisitions/{requisition_id}/open",
        "/requisitions/{requisition_id}/cancel",
    ),
    actions=("requisitions.manage",),
    scope=ResourceScope.HIRING_ASSIGNMENT,
)
_register(
    "POST",
    ("/requisitions/{requisition_id}/vote",),
    actions=("requisitions.approve",),
    scope=ResourceScope.HIRING_ASSIGNMENT,
)
_register(
    "GET",
    ("/offer-templates",),
    actions=("offers.read",),
    scope=ResourceScope.HIRING_ASSIGNMENT,
    fields=(FieldClass.OFFER,),
)
_register(
    "GET",
    ("/candidates/{candidate_id}/offers",),
    actions=("offers.read",),
    scope=ResourceScope.HIRING_ASSIGNMENT,
    # The response is server-redacted for compensation and offer body fields;
    # it does not expose candidate contact data. Assigned hiring managers may
    # therefore inspect offer state without gaining CONTACT field access.
    fields=(FieldClass.OFFER,),
)
_register(
    ("POST", "PATCH"),
    (
        "/offer-templates",
        "/offer-templates/{template_id}",
    ),
    actions=("offers.manage",),
    scope=ResourceScope.HIRING_ASSIGNMENT,
    fields=(FieldClass.OFFER,),
)
_register(
    ("POST", "PATCH"),
    (
        "/candidates/{candidate_id}/offers",
        "/offers/{offer_id}",
        "/offers/{offer_id}/send",
        "/offers/{offer_id}/submit",
        "/offers/{offer_id}/rotate",
        "/offers/{offer_id}/withdraw",
    ),
    actions=("offers.manage",),
    scope=ResourceScope.HIRING_ASSIGNMENT,
    # These operations return the server-redacted offer projection or an
    # opaque capability URL; none returns candidate contact fields. Sending is
    # an explicit offer action and does not disclose the candidate's address
    # to the caller, so CONTACT must not block an assigned hiring manager.
    fields=(FieldClass.OFFER,),
)
_register(
    "POST",
    ("/offers/{offer_id}/vote",),
    actions=("offers.manage",),
    scope=ResourceScope.HIRING_ASSIGNMENT,
    fields=(FieldClass.OFFER,),
)
_register(
    "GET",
    ("/candidates/{candidate_id}/handoffs",),
    actions=("offers.read",),
    scope=ResourceScope.HIRING_ASSIGNMENT,
)
_register(
    "PATCH",
    ("/handoffs/{handoff_id}",),
    actions=("offers.manage",),
    scope=ResourceScope.HIRING_ASSIGNMENT,
)

# Scheduling is either self-owned or candidate-assignment scoped.
_register(
    "GET",
    (
        "/calendar/appointments/me",
        "/calendar/availability/blocks",
        "/calendar/availability/me",
        "/calendar/providers",
    ),
    actions=("scheduling.read",),
    scope=ResourceScope.SELF,
)
_register(
    ("POST", "PUT", "DELETE"),
    (
        "/calendar/availability/blocks",
        "/calendar/availability/blocks/{block_id}",
        "/calendar/availability/me",
    ),
    actions=("scheduling.manage",),
    scope=ResourceScope.SELF,
)
_register(
    "GET",
    (
        "/candidates/{candidate_id}/appointments",
        "/candidates/{candidate_id}/scheduling-links",
    ),
    actions=("scheduling.read",),
    scope=ResourceScope.HIRING_ASSIGNMENT,
)
_register(
    "POST",
    ("/candidates/{candidate_id}/scheduling-links",),
    actions=("scheduling.manage", "candidates.contact"),
    scope=ResourceScope.HIRING_ASSIGNMENT,
    fields=(FieldClass.CONTACT,),
)
_register(
    "DELETE",
    ("/scheduling-links/{link_id}",),
    actions=("scheduling.manage",),
    scope=ResourceScope.HIRING_ASSIGNMENT,
)
_register(
    "POST",
    ("/scheduling-links/{link_id}/rotate",),
    actions=("scheduling.manage",),
    scope=ResourceScope.HIRING_ASSIGNMENT,
)
_register(
    ("POST", "DELETE"),
    (
        "/calendar/appointments/{appointment_id}/manage-token/rotate",
        "/calendar/appointments/{appointment_id}/manage-token",
    ),
    actions=("scheduling.manage",),
    scope=ResourceScope.HIRING_ASSIGNMENT,
)

# AI surfaces.  Business-context sessions require the dedicated sensitive
# context permission in addition to ordinary model use.
_register(
    "GET",
    (
        "/ai/v1/action-bundles",
        "/ai/v1/missions",
        "/ai/v1/missions/{mission_id}",
        "/ai/v1/missions/{mission_id}/candidates",
        "/ai/v1/missions/{mission_id}/events",
        "/ai/v1/sessions",
        "/ai/v1/sessions/{session_id}",
        "/ai/v1/sessions/{session_id}/attachments/{attachment_id}",
        "/chat/capability",
        "/chat/sessions",
        "/chat/sessions/{session_id}",
    ),
    actions=("ai.use", "ai.sensitive_context"),
    scope=ResourceScope.HIRING_ASSIGNMENT,
    fields=(
        FieldClass.AI_CONTEXT,
        FieldClass.CONTACT,
        FieldClass.CANDIDATE_DOCUMENT,
        FieldClass.ASSESSMENT_RESULT,
        FieldClass.COMPENSATION,
    ),
)
_register(
    "POST",
    (
        "/ai/v1/action-bundles/{bundle_id}/approve",
        "/ai/v1/action-bundles/{bundle_id}/reject",
        "/ai/v1/missions/{mission_id}/replan",
        "/ai/v1/missions/{mission_id}/retry",
        "/ai/v1/missions/{mission_id}/stop",
        "/ai/v1/sessions",
        "/ai/v1/sessions/{session_id}/archive",
        "/ai/v1/sessions/{session_id}/attachments",
        "/ai/v1/sessions/{session_id}/messages",
        "/chat/sessions",
        "/chat/sessions/{session_id}/messages",
        "/chat/sessions/{session_id}/messages/stream",
    ),
    actions=("ai.use", "ai.sensitive_context"),
    scope=ResourceScope.HIRING_ASSIGNMENT,
    fields=(
        FieldClass.AI_CONTEXT,
        FieldClass.CONTACT,
        FieldClass.CANDIDATE_DOCUMENT,
        FieldClass.ASSESSMENT_RESULT,
        FieldClass.COMPENSATION,
    ),
)
_register(
    "DELETE",
    ("/ai/v1/sessions/{session_id}/attachments/{attachment_id}",),
    actions=("ai.use", "ai.sensitive_context"),
    scope=ResourceScope.HIRING_ASSIGNMENT,
    fields=(
        FieldClass.AI_CONTEXT,
        FieldClass.CONTACT,
        FieldClass.CANDIDATE_DOCUMENT,
        FieldClass.ASSESSMENT_RESULT,
        FieldClass.COMPENSATION,
    ),
)
_register("GET", ("/ai/v1/settings",), actions=("ai.manage",))
_register("PUT", ("/ai/v1/settings",), actions=("ai.manage",))
_register(
    "POST",
    ("/candidates/{candidate_id}/score", "/candidates/{candidate_id}/score-all-open"),
    actions=("candidates.write", "ai.use", "ai.sensitive_context"),
    scope=ResourceScope.HIRING_ASSIGNMENT,
    fields=(
        FieldClass.AI_CONTEXT,
        FieldClass.CONTACT,
        FieldClass.CANDIDATE_DOCUMENT,
        FieldClass.ASSESSMENT_RESULT,
        FieldClass.COMPENSATION,
    ),
)

# Analytics, workflows, sourcing, talent CRM, and external sharing.
_register(
    "GET",
    (
        "/analytics/applications",
        "/analytics/applications/reconciliation",
        "/analytics/pipeline",
        "/sourcing/analytics",
    ),
    actions=("analytics.read",),
)
_register(
    "GET",
    ("/workflows/rules", "/workflows/rules/{rule_id}", "/workflows/runs"),
    actions=("workflows.read",),
    scope=ResourceScope.HIRING_ASSIGNMENT,
)
_register(
    ("POST", "PATCH", "DELETE"),
    (
        "/workflows/rules",
        "/workflows/rules/{rule_id}",
        "/workflows/rules/{rule_id}/pause",
        "/workflows/rules/{rule_id}/resume",
        "/workflows/rules/{rule_id}/dry-run",
    ),
    actions=("workflows.manage",),
    scope=ResourceScope.HIRING_ASSIGNMENT,
)
_register(
    "POST",
    (
        "/workflows/process",
        "/workflows/runs/{run_id}/replay",
        "/workflows/runs/{run_id}/cancel",
    ),
    actions=("workflows.execute",),
    scope=ResourceScope.HIRING_ASSIGNMENT,
)
_register(
    "GET",
    ("/source-integrations", "/source-integrations/runs"),
    actions=("integrations.read",),
)
_register(
    ("POST", "PATCH"),
    ("/source-integrations", "/source-integrations/{integration_id}"),
    actions=("integrations.manage",),
)
_register(
    "POST",
    (
        "/source-integrations/{integration_id}/import",
        "/source-integrations/{integration_id}/import-file",
        "/source-integrations/{integration_id}/import-file/preview",
        "/source-integrations/{integration_id}/hh/imports",
    ),
    actions=("sourcing.manage",),
)
_register(
    "POST",
    (
        "/source-integrations/{integration_id}/publications",
        "/source-integrations/{integration_id}/publications/preview",
    ),
    actions=("sourcing.publish",),
)
_register(
    "POST",
    ("/source-integrations/{integration_id}/rotate-webhook-secret",),
    actions=("integrations.manage", "integrations.credentials"),
    fields=(FieldClass.SECRET,),
)
_register(
    "GET",
    ("/talent/nurture-sequences", "/talent/pools", "/talent/pools/{pool_id}/members"),
    actions=("talent.read",),
    scope=ResourceScope.HIRING_ASSIGNMENT,
)
_register(
    "GET",
    ("/talent/search", "/positions/{position_id}/talent-matches"),
    actions=("talent.read",),
    scope=ResourceScope.HIRING_ASSIGNMENT,
    fields=(FieldClass.CONTACT, FieldClass.CANDIDATE_DOCUMENT),
)
_register(
    ("POST", "PATCH", "DELETE"),
    (
        "/talent/nurture-sequences",
        "/talent/pools",
        "/talent/pools/{pool_id}",
        "/talent/pools/{pool_id}/members",
        "/talent/pools/{pool_id}/members/{candidate_id}",
    ),
    actions=("talent.manage",),
    scope=ResourceScope.HIRING_ASSIGNMENT,
)
_register(
    "POST",
    ("/talent/nurture-sequences/{sequence_id}/launch",),
    actions=("talent.manage", "communications.send", "candidates.contact"),
    scope=ResourceScope.HIRING_ASSIGNMENT,
    fields=(FieldClass.CONTACT,),
)
_register(
    "GET",
    ("/positions/{position_id}/referral-links",),
    actions=("sourcing.read",),
    scope=ResourceScope.HIRING_ASSIGNMENT,
)
_register(
    "POST",
    (
        "/positions/{position_id}/referral-links",
        "/referral-links/{link_id}/revoke",
        "/referral-links/{link_id}/rotate",
    ),
    actions=("sourcing.manage",),
    scope=ResourceScope.HIRING_ASSIGNMENT,
)
_register(
    "GET",
    ("/positions/{position_id}/shortlists",),
    actions=("candidates.export",),
    scope=ResourceScope.HIRING_ASSIGNMENT,
    fields=(FieldClass.EXPORT, FieldClass.ASSESSMENT_RESULT),
)
_register(
    "POST",
    (
        "/positions/{position_id}/shortlists",
        "/shortlists/{shortlist_id}/revoke",
        "/shortlists/{shortlist_id}/rotate",
    ),
    actions=("candidates.export",),
    scope=ResourceScope.HIRING_ASSIGNMENT,
    fields=(FieldClass.EXPORT, FieldClass.ASSESSMENT_RESULT),
)

# Organization settings, members, personal work, and notifications.
_register("GET", ("/organizations/me",), actions=("enterprise.read",))
_register(
    ("PATCH", "POST", "DELETE"),
    ("/organizations/me", "/organizations/me/logo"),
    actions=("enterprise.manage",),
)
_register("GET", ("/users", "/users/assignable"), actions=("enterprise.read",))
_register(
    ("POST", "DELETE"),
    (
        "/users",
        "/users/invitations",
        "/users/{user_id}",
        "/users/{user_id}/password-reset",
        "/users/access-tokens/{token_id}/revoke",
    ),
    actions=("enterprise.manage",),
)
_register(
    "PATCH",
    ("/users/{user_id}",),
    actions=("account.manage",),
    scope=ResourceScope.SELF,
)
_register(
    "POST",
    ("/users/me/password",),
    actions=("account.manage",),
    scope=ResourceScope.SELF,
)
_register(
    "GET",
    ("/my-work", "/notifications"),
    actions=("account.read",),
    scope=ResourceScope.SELF,
)
_register(
    "POST",
    ("/notifications/read-all", "/notifications/{notification_id}/read"),
    actions=("account.manage",),
    scope=ResourceScope.SELF,
)
_register(
    "GET",
    ("/folders",),
    actions=("candidates.read",),
    scope=ResourceScope.ORGANIZATION,
)
_register(
    ("POST", "PATCH", "DELETE"),
    ("/folders", "/folders/reorder", "/folders/{folder_id}"),
    actions=("candidates.write",),
    scope=ResourceScope.ORGANIZATION,
)

# Enterprise governance.  Read-only self-service policy/entitlement views are
# separate from audit, backup, credential, and mutation permissions.
_register(
    "GET",
    ("/enterprise/policy", "/enterprise/entitlements"),
    actions=("enterprise.read",),
)
_register(
    "GET",
    ("/enterprise/permissions/me",),
    scope=ResourceScope.SELF,
)
_register("GET", ("/enterprise/usage",), actions=("enterprise.manage",))
_register(
    "GET",
    ("/enterprise/operations/readiness",),
    actions=("enterprise.manage",),
)
_register(
    "GET",
    ("/enterprise/permissions", "/enterprise/retention/preview"),
    actions=("enterprise.manage",),
)
_register(
    "GET",
    ("/enterprise/trash",),
    actions=("enterprise.manage",),
    fields=(FieldClass.CONTACT,),
)
_register(
    ("PUT", "PATCH", "POST", "DELETE"),
    (
        "/enterprise/policy",
        "/enterprise/permissions",
        "/enterprise/permissions/{override_id}",
        "/enterprise/retention/run",
        "/enterprise/trash/{resource_type}/{resource_id}/restore",
    ),
    actions=("enterprise.manage",),
)
_register(
    "GET",
    ("/enterprise/audit",),
    actions=("enterprise.audit_export",),
    fields=(FieldClass.AUDIT,),
)
_register(
    "GET",
    ("/enterprise/backup",),
    actions=("enterprise.backup_export",),
    fields=(
        FieldClass.BACKUP,
        FieldClass.CANDIDATE_DOCUMENT,
        FieldClass.CONTACT,
        FieldClass.ASSESSMENT_RESULT,
    ),
)
_register(
    "GET",
    ("/enterprise/api-keys", "/enterprise/sso", "/enterprise/model-configuration"),
    actions=("integrations.read",),
    fields=(FieldClass.SECRET,),
)
_register(
    ("POST", "PUT"),
    (
        "/enterprise/api-keys",
        "/enterprise/api-keys/{api_key_id}/revoke",
        "/enterprise/sso",
    ),
    actions=("integrations.manage", "integrations.credentials"),
    fields=(FieldClass.SECRET,),
)
_register(
    "PUT",
    ("/enterprise/model-configuration",),
    actions=("ai.manage", "ai.managed_provider"),
    fields=(FieldClass.SECRET,),
)

# Provider-agnostic integration control plane.
_register(
    "GET",
    (
        "/integrations",
        "/integrations/{connection_id}",
        "/integrations/providers",
        "/integrations/jobs",
        "/integrations/jobs/{job_id}",
        "/integrations/deliveries",
        "/integrations/deliveries/{delivery_id}",
    ),
    actions=("integrations.read",),
)
_register(
    ("POST", "PATCH"),
    (
        "/integrations",
        "/integrations/{connection_id}",
        "/integrations/{connection_id}/health",
        "/integrations/{connection_id}/revoke",
        "/integrations/{connection_id}/jobs",
        "/integrations/jobs/{job_id}/cancel",
        "/integrations/jobs/{job_id}/replay",
        "/integrations/jobs/{job_id}/run",
        "/integrations/{connection_id}/deliveries",
        "/integrations/deliveries/{delivery_id}/attempt",
    ),
    actions=("integrations.manage",),
)
_register(
    "PUT",
    ("/integrations/{connection_id}/credentials",),
    actions=("integrations.manage", "integrations.credentials"),
    fields=(FieldClass.SECRET,),
)
_register(
    "POST",
    ("/integrations/{connection_id}/oauth/start",),
    actions=("integrations.manage", "integrations.credentials"),
    fields=(FieldClass.SECRET,),
)
_register(
    "GET",
    ("/integrations/assessment/connections",),
    actions=("assessments.read",),
)
_register(
    "POST",
    (
        "/integrations/{connection_id}/assessment/invitations",
        "/integrations/{connection_id}/assessment/attempts/status",
    ),
    actions=("assessments.manage",),
    scope=ResourceScope.HIRING_ASSIGNMENT,
)
_register(
    "POST",
    ("/integrations/{connection_id}/assessment/attempts/result",),
    actions=("assessments.results",),
    scope=ResourceScope.HIRING_ASSIGNMENT,
    fields=(FieldClass.ASSESSMENT_RESULT,),
)
_register(
    "GET",
    ("/integrations/assessment/applications/{application_id}/evidence",),
    actions=("assessments.results",),
    scope=ResourceScope.HIRING_ASSIGNMENT,
    fields=(FieldClass.ASSESSMENT_RESULT,),
)
_register(
    "POST",
    ("/integrations/oauth/callback",),
    auth_mode=AuthMode.CAPABILITY_TOKEN,
)
_register(
    "POST",
    ("/integrations/webhooks/{connection_id}",),
    auth_mode=AuthMode.WEBHOOK_SECRET,
)


# Exact reviewed surface.  The registration blocks above express shared
# policy, while this allowlist prevents a future method added to an existing
# path from inheriting policy accidentally.
_BASE_KNOWN_OPERATION_KEYS = frozenset(
    tuple(line.split(" ", 1))
    for line in """
DELETE /applications/{application_id}
DELETE /ai/v1/sessions/{session_id}/attachments/{attachment_id}
DELETE /auth/sessions/{session_id}
DELETE /calendar/availability/blocks/{block_id}
DELETE /calendar/appointments/{appointment_id}/manage-token
DELETE /candidate-views/{view_id}
DELETE /candidates/{candidate_id}
DELETE /candidates/{candidate_id}/github
DELETE /candidates/{candidate_id}/notes/{note_id}
DELETE /candidates/{candidate_id}/tags/{tag_id}
DELETE /enterprise/permissions/{override_id}
DELETE /folders/{folder_id}
DELETE /interviews/{interview_id}
DELETE /organizations/me/logo
DELETE /positions/{position_id}
DELETE /public/assessments/{token}/attempts/{attempt_id}/files/{file_id}
DELETE /recordings/{recording_id}
DELETE /recordings/{recording_id}/file
DELETE /scheduling-links/{link_id}
DELETE /talent/pools/{pool_id}
DELETE /talent/pools/{pool_id}/members/{candidate_id}
DELETE /tasks/{task_id}
DELETE /tasks/{task_id}/file
DELETE /users/{user_id}
DELETE /workflows/rules/{rule_id}
GET /ai/v1/action-bundles
GET /ai/v1/missions
GET /ai/v1/missions/{mission_id}
GET /ai/v1/missions/{mission_id}/candidates
GET /ai/v1/missions/{mission_id}/events
GET /ai/v1/sessions
GET /ai/v1/sessions/{session_id}
GET /ai/v1/sessions/{session_id}/attachments/{attachment_id}
GET /ai/v1/settings
GET /analytics/applications
GET /analytics/applications/reconciliation
GET /analytics/pipeline
GET /applications
GET /applications/{application_id}
GET /applications/{application_id}/assessment-invitations
GET /assessment-attempts/{attempt_id}
GET /assessment-files/{file_id}/download
GET /assessment-invitations/{invitation_id}/attempts
GET /assessment-templates
GET /assessment-templates/{template_id}
GET /auth/me
GET /auth/invitations/{token}
GET /auth/password-reset/{token}
GET /auth/sessions
GET /calendar/appointments/me
GET /calendar/availability/blocks
GET /calendar/availability/me
GET /calendar/providers
GET /candidate-duplicates
GET /candidate-search
GET /candidate-views
GET /chat/capability
GET /candidates
GET /candidates/{candidate_id}
GET /candidates/{candidate_id}/applications
GET /candidates/{candidate_id}/activities
GET /candidates/{candidate_id}/appointments
GET /candidates/{candidate_id}/best-positions
GET /candidates/{candidate_id}/communication-threads
GET /candidates/{candidate_id}/contact-points
GET /candidates/{candidate_id}/cv
GET /candidates/{candidate_id}/github/detect
GET /candidates/{candidate_id}/handoffs
GET /candidates/{candidate_id}/interviews
GET /candidates/{candidate_id}/merged-sources
GET /candidates/{candidate_id}/merge-history
GET /candidates/{candidate_id}/merged-sources/{source_candidate_id}/cv
GET /candidates/{candidate_id}/merged-sources/{source_candidate_id}/evidence
GET /candidates/{candidate_id}/notes
GET /candidates/{candidate_id}/offers
GET /candidates/{candidate_id}/processing-jobs/{job_id}
GET /candidates/{candidate_id}/recordings
GET /candidates/{candidate_id}/scheduling-links
GET /candidates/{candidate_id}/scorecards
GET /candidates/{candidate_id}/tags
GET /candidates/{candidate_id}/tasks
GET /chat/sessions
GET /chat/sessions/{session_id}
GET /communications/capabilities
GET /communications/accounts
GET /communications/templates
GET /communications/threads
GET /communications/threads/{thread_id}/messages
GET /enterprise/api-keys
GET /enterprise/api/v1/candidates
GET /enterprise/api/v1/positions
GET /enterprise/audit
GET /enterprise/backup
GET /enterprise/entitlements
GET /enterprise/operations/readiness
GET /enterprise/model-configuration
GET /enterprise/permissions
GET /enterprise/permissions/me
GET /enterprise/policy
GET /enterprise/retention/preview
GET /enterprise/sso
GET /enterprise/trash
GET /enterprise/usage
GET /folders
GET /health
GET /health/live
GET /health/ready
GET /interviews/{interview_id}
GET /my-work
GET /notifications
GET /offer-templates
GET /organizations/me
GET /positions
GET /positions/{position_id}
GET /positions/{position_id}/applications
GET /positions/{position_id}/referral-links
GET /positions/{position_id}/rescore-capability
GET /positions/{position_id}/shortlists
GET /positions/{position_id}/talent-matches
GET /public/assessments/{token}
GET /public/offers/{token}
GET /public/positions/{position_id}
GET /public/referrals/{token}
GET /public/scheduling/manage/{manage_token}
GET /public/scheduling/manage/{manage_token}/slots
GET /public/scheduling/{token}
GET /public/scheduling/{token}/slots
GET /public/shortlists/{token}
GET /public/v1/careers/widget.js
GET /public/v1/careers/{organization_slug}/positions
GET /public/v1/careers/{organization_slug}/positions/{position_id}
GET /public/v1/careers/{organization_slug}/preview
GET /recordings/{recording_id}
GET /recordings/{recording_id}/file
GET /requisitions
GET /scorecard-templates
GET /source-integrations
GET /source-integrations/runs
GET /sourcing/analytics
GET /talent/nurture-sequences
GET /talent/pools
GET /talent/pools/{pool_id}/members
GET /talent/search
GET /tasks/{task_id}
GET /tasks/{task_id}/file
GET /users
GET /users/assignable
GET /workflows/rules
GET /workflows/rules/{rule_id}
GET /workflows/runs
PATCH /applications/{application_id}
PATCH /assessment-templates/{template_id}
PATCH /candidate-views/{view_id}
PATCH /candidates/{candidate_id}
PATCH /candidates/{candidate_id}/contact-points/{contact_point_id}
PATCH /communications/templates/{template_id}
PATCH /enterprise/policy
PATCH /folders/reorder
PATCH /folders/{folder_id}
PATCH /handoffs/{handoff_id}
PATCH /interviews/{interview_id}
PATCH /interviews/{interview_id}/questions/{question_id}
PATCH /offer-templates/{template_id}
PATCH /offers/{offer_id}
PATCH /organizations/me
PATCH /positions/move
PATCH /positions/{position_id}
PATCH /positions/{position_id}/candidates/{candidate_id}/stage
PATCH /positions/{position_id}/candidates/{candidate_id}/work
PATCH /public/assessments/{token}/attempts/{attempt_id}/answers
PATCH /recordings/{recording_id}
PATCH /requisitions/{requisition_id}
PATCH /scorecard-assignments/{assignment_id}
PATCH /scorecard-templates/{template_id}
PATCH /source-integrations/{integration_id}
PATCH /talent/pools/{pool_id}
PATCH /tasks/{task_id}
PATCH /users/{user_id}
PATCH /workflows/rules/{rule_id}
POST /ai/v1/action-bundles/{bundle_id}/approve
POST /ai/v1/action-bundles/{bundle_id}/reject
POST /ai/v1/missions/{mission_id}/replan
POST /ai/v1/missions/{mission_id}/retry
POST /ai/v1/missions/{mission_id}/stop
POST /ai/v1/sessions
POST /ai/v1/sessions/{session_id}/archive
POST /ai/v1/sessions/{session_id}/attachments
POST /ai/v1/sessions/{session_id}/messages
POST /applications
POST /applications/{application_id}/assessment-invitations
POST /applications/{application_id}/stage
POST /assessment-attempts/{attempt_id}/manual-grade
POST /assessment-invitations/{invitation_id}/reissue
POST /assessment-invitations/{invitation_id}/retakes
POST /assessment-invitations/{invitation_id}/revoke
POST /assessment-templates
POST /assessment-templates/{template_id}/versions
POST /auth/login
POST /auth/invitations/{token}/accept
POST /auth/logout
POST /auth/logout-all
POST /auth/mfa/confirm
POST /auth/mfa/disable
POST /auth/mfa/setup
POST /auth/mfa/verify
POST /auth/refresh
POST /auth/password-reset/request
POST /auth/password-reset/{token}
POST /calendar/availability/blocks
POST /calendar/appointments/{appointment_id}/manage-token/rotate
POST /candidate-bulk
POST /candidate-export
POST /candidate-merges/preview
POST /candidate-merges/{batch_id}/reverse
POST /candidate-views
POST /candidates/upload/file
POST /candidates/upload/hh
POST /candidates/upload/text
POST /candidates/{candidate_id}/communications/preview
POST /candidates/{candidate_id}/communications/send
POST /candidates/{candidate_id}/contact-points
POST /candidates/{candidate_id}/decisions
POST /candidates/{candidate_id}/interviews
POST /candidates/{candidate_id}/interviews/generate
POST /candidates/{candidate_id}/notes
POST /candidates/{candidate_id}/offers
POST /candidates/{candidate_id}/recordings
POST /candidates/{candidate_id}/scheduling-links
POST /candidates/{candidate_id}/score
POST /candidates/{candidate_id}/score-all-open
POST /candidates/{candidate_id}/scorecards/assign
POST /candidates/{candidate_id}/tags
POST /candidates/{candidate_id}/tasks
POST /candidates/{candidate_id}/view
POST /candidates/{primary_candidate_id}/merge
POST /chat/sessions
POST /chat/sessions/{session_id}/messages
POST /chat/sessions/{session_id}/messages/stream
POST /communications/accounts
POST /communications/accounts/{account_id}/rotate-webhook-secret
POST /communications/bulk
POST /communications/bulk/preview
POST /communications/messages/{message_id}/cancel
POST /communications/templates
POST /communications/threads/{thread_id}/read
POST /communications/webhooks/sandbox/{account_id}
POST /enterprise/api-keys
POST /enterprise/api-keys/{api_key_id}/revoke
POST /enterprise/retention/run
POST /enterprise/trash/{resource_type}/{resource_id}/restore
POST /folders
POST /hiring-decisions/{decision_id}/vote
POST /interviews/{interview_id}/complete
POST /interviews/{interview_id}/kit-versions
POST /notifications/read-all
POST /notifications/{notification_id}/read
POST /offer-templates
POST /offers/{offer_id}/send
POST /offers/{offer_id}/submit
POST /offers/{offer_id}/rotate
POST /offers/{offer_id}/vote
POST /offers/{offer_id}/withdraw
POST /organizations/me/logo
POST /positions
POST /positions/draft
POST /positions/draft/extract-text
POST /positions/project
POST /positions/project/extract
POST /positions/{position_id}/generate-profile
POST /positions/{position_id}/referral-links
POST /positions/{position_id}/rescore
POST /positions/{position_id}/shortlists
POST /public/assessments/{token}/attempts
POST /public/assessments/{token}/attempts/{attempt_id}/questions/{question_id}/files
POST /public/assessments/{token}/attempts/{attempt_id}/submit
POST /public/offers/{token}/respond
POST /public/positions/{position_id}/apply
POST /public/scheduling/manage/{manage_token}/cancel
POST /public/scheduling/manage/{manage_token}/reschedule
POST /public/scheduling/{token}/book
POST /public/v1/careers/{organization_slug}/positions/{position_id}/apply
POST /referral-links/{link_id}/revoke
POST /referral-links/{link_id}/rotate
POST /recordings/{recording_id}/file
POST /requisitions
POST /requisitions/{requisition_id}/cancel
POST /requisitions/{requisition_id}/open
POST /requisitions/{requisition_id}/submit
POST /requisitions/{requisition_id}/vote
POST /scorecard-assignments/{assignment_id}/submit
POST /scorecard-templates
POST /scheduling-links/{link_id}/rotate
POST /shortlists/{shortlist_id}/revoke
POST /shortlists/{shortlist_id}/rotate
POST /source-integrations
POST /source-integrations/{integration_id}/import
POST /source-integrations/{integration_id}/import-file
POST /source-integrations/{integration_id}/hh/imports
POST /source-integrations/{integration_id}/publications
POST /source-integrations/{integration_id}/publications/preview
POST /source-integrations/{integration_id}/rotate-webhook-secret
POST /source-webhooks/{integration_id}
POST /talent/nurture-sequences
POST /talent/nurture-sequences/{sequence_id}/launch
POST /talent/pools
POST /talent/pools/{pool_id}/members
POST /tasks/{task_id}/evaluate
POST /tasks/{task_id}/file
POST /users
POST /users/access-tokens/{token_id}/revoke
POST /users/invitations
POST /users/me/password
POST /users/{user_id}/password-reset
POST /workflows/process
POST /workflows/rules
POST /workflows/rules/{rule_id}/dry-run
POST /workflows/rules/{rule_id}/pause
POST /workflows/rules/{rule_id}/resume
POST /workflows/runs/{run_id}/cancel
POST /workflows/runs/{run_id}/replay
PUT /ai/v1/settings
PUT /calendar/availability/me
PUT /candidates/{candidate_id}/github
PUT /enterprise/model-configuration
PUT /enterprise/permissions
PUT /enterprise/sso
""".splitlines()
    if line
)
KNOWN_OPERATION_KEYS = _BASE_KNOWN_OPERATION_KEYS | frozenset(
    {
        ("GET", "/integrations"),
        ("GET", "/integrations/assessment/connections"),
        ("GET", "/integrations/assessment/applications/{application_id}/evidence"),
        ("GET", "/integrations/deliveries"),
        ("GET", "/integrations/deliveries/{delivery_id}"),
        ("GET", "/integrations/jobs"),
        ("GET", "/integrations/jobs/{job_id}"),
        ("GET", "/integrations/providers"),
        ("GET", "/integrations/{connection_id}"),
        ("PATCH", "/integrations/{connection_id}"),
        ("DELETE", "/integrations/{connection_id}/calendar/events"),
        ("POST", "/integrations"),
        ("POST", "/integrations/deliveries/{delivery_id}/attempt"),
        ("POST", "/integrations/jobs/{job_id}/cancel"),
        ("POST", "/integrations/jobs/{job_id}/replay"),
        ("POST", "/integrations/jobs/{job_id}/run"),
        ("POST", "/integrations/oauth/callback"),
        ("POST", "/integrations/webhooks/{connection_id}"),
        ("POST", "/integrations/{connection_id}/assessment/attempts/result"),
        ("POST", "/integrations/{connection_id}/assessment/attempts/status"),
        ("POST", "/integrations/{connection_id}/assessment/invitations"),
        ("POST", "/integrations/{connection_id}/calendar/events"),
        ("POST", "/integrations/{connection_id}/deliveries"),
        ("POST", "/integrations/{connection_id}/email/messages"),
        ("POST", "/integrations/{connection_id}/health"),
        ("POST", "/integrations/{connection_id}/jobs"),
        ("POST", "/integrations/{connection_id}/hh/imports"),
        ("POST", "/integrations/{connection_id}/hh/vacancies"),
        ("POST", "/integrations/{connection_id}/oauth/start"),
        ("POST", "/integrations/{connection_id}/revoke"),
        ("POST", "/integrations/{connection_id}/telegram/messages"),
        ("POST", "/source-integrations/{integration_id}/import-file/preview"),
        ("PUT", "/integrations/{connection_id}/credentials"),
        ("PUT", "/integrations/{connection_id}/calendar/events"),
    }
)
_missing_operation_policies = KNOWN_OPERATION_KEYS - _catalog.keys()
if _missing_operation_policies:
    raise RuntimeError(
        "Known operations lack authorization policy: "
        + ", ".join(
            f"{method} {path}" for method, path in sorted(_missing_operation_policies)
        )
    )
OPERATION_CATALOG = MappingProxyType(
    {key: _catalog[key] for key in sorted(KNOWN_OPERATION_KEYS)}
)


def operation_policy(method: str, path_template: str) -> OperationPolicy | None:
    """Return the reviewed policy for an exact HTTP method/path template."""

    return OPERATION_CATALOG.get((method.upper(), path_template))
