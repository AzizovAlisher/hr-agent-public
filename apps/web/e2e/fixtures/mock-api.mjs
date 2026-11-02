import { createServer } from "node:http";

const port = Number(process.env.MOCK_API_PORT ?? "4100");
const attempts = new Map();
const assessmentEvents = new Map();
const assessmentReviewStates = new Map();
const assessmentReviewRequests = new Map();
const offerResponses = new Map();
const projectCreationTokens = new Set();
let replayRefreshAttempts = 0;
let logoutRequests = [];
const brandingOrganizationId = "40000000-0000-4000-8000-000000000001";
const brandingVersionId = "40000000-0000-4000-8000-000000000002";
const interviewerOrganizationId = "50000000-0000-4000-8000-000000000001";
const interviewerUserId = "50000000-0000-4000-8000-000000000002";
const adminUserId = "50000000-0000-4000-8000-000000000007";
const recruiterUserId = "50000000-0000-4000-8000-000000000008";
const legacyWhatsAppTemplateId = "50000000-0000-4000-8000-000000000009";
const draftOrganizationAId = "60000000-0000-4000-8000-000000000001";
const draftUserAId = "60000000-0000-4000-8000-000000000002";
const draftOrganizationBId = "60000000-0000-4000-8000-000000000003";
const draftUserBId = "60000000-0000-4000-8000-000000000004";
const readOnlyPositionId = "50000000-0000-4000-8000-000000000003";
const readOnlyCandidateId = "50000000-0000-4000-8000-000000000004";
const readOnlyScoreId = "50000000-0000-4000-8000-000000000005";
const readOnlyFolderId = "50000000-0000-4000-8000-000000000006";
const pixelPng = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
  "base64",
);

const IDS = {
  invitation: "10000000-0000-4000-8000-000000000001",
  section: "10000000-0000-4000-8000-000000000002",
  consentQuestion: "10000000-0000-4000-8000-000000000003",
  noteQuestion: "10000000-0000-4000-8000-000000000004",
  numericQuestion: "10000000-0000-4000-8000-000000000008",
  numericBranchQuestion: "10000000-0000-4000-8000-000000000009",
  singleQuestion: "10000000-0000-4000-8000-000000000010",
  multiQuestion: "10000000-0000-4000-8000-000000000011",
  longQuestion: "10000000-0000-4000-8000-000000000012",
  urlQuestion: "10000000-0000-4000-8000-000000000013",
  fileQuestion: "10000000-0000-4000-8000-000000000014",
  assessmentFile: "10000000-0000-4000-8000-000000000015",
  assessmentTemplate: "10000000-0000-4000-8000-000000000016",
  assessmentVersion: "10000000-0000-4000-8000-000000000017",
  assessmentResult: "10000000-0000-4000-8000-000000000018",
  attempt: "10000000-0000-4000-8000-000000000005",
  appointment: "20000000-0000-4000-8000-000000000001",
};

const AI_SESSION_OK = "80000000-0000-4000-8000-000000000001";
const AI_SESSION_ERROR = "80000000-0000-4000-8000-000000000002";
const AI_UPSTREAM_SECRET = "UPSTREAM_AI_SECRET_CANARY_MUST_NOT_REACH_BROWSER";

const fixtureTimestamp = "2026-08-20T09:00:00.000Z";

function defaultEnterprisePolicy() {
  return {
    candidate_retention_days: 365,
    deleted_retention_days: 30,
    audit_retention_days: 2555,
    candidate_export_enabled: true,
    backup_export_enabled: true,
    approval_separation_of_duties: {
      requisitions: true,
      offers: true,
      hiring_decisions: true,
    },
    field_visibility: {
      compensation: ["admin", "hiring_manager"],
    },
    updated_at: fixtureTimestamp,
  };
}

let enterprisePolicy = defaultEnterprisePolicy();

function readOnlyPosition({ detail = false } = {}) {
  const position = {
    id: readOnlyPositionId,
    title: "Read-only Platform Engineer",
    description: "A populated fixture used to prove interviewer read access.",
    requirements: {
      company: "Northstar Hiring",
      skills: ["TypeScript", "Python"],
    },
    internal_notes: "Interviewers may read this note but cannot edit it.",
    status: "open",
    full_description:
      "Build reliable hiring workflows while preserving tenant boundaries.",
    folder: "Pilot Engineering",
    folder_id: readOnlyFolderId,
    public_apply_enabled: true,
    sensitivity: "public",
    candidate_profile:
      "A pragmatic engineer who communicates clearly and values accessibility.",
    public_description: null,
    created_by: interviewerUserId,
    candidate_count: 1,
    chat_session_count: 2,
    created_at: fixtureTimestamp,
    updated_at: fixtureTimestamp,
  };
  if (!detail) return position;
  return {
    ...position,
    candidates: [
      {
        candidate_id: readOnlyCandidateId,
        application_id: readOnlyScoreId,
        application_version: fixtureTimestamp,
        source: "referral",
        source_ref: "read-only-candidate@example.test",
        overall_score: 84,
        score_technical_skills: 88,
        score_quality_of_projects: 82,
        score_previous_experience: 83,
        score_impact: 81,
        comment_technical_skills: "Strong fundamentals.",
        comment_quality_of_projects: "Relevant delivery history.",
        comment_previous_experience: "Appropriate scope.",
        comment_impact: "Clear outcomes.",
        model_used: "fixture-model",
        is_favorite: false,
        viewed_at: null,
        stage: "interview",
        owner_user_id: interviewerUserId,
        next_action_due_date: "2026-08-25",
      },
    ],
  };
}

function projectPositions() {
  return [
    {
      ...readOnlyPosition(),
      id: "70000000-0000-4000-8000-000000000001",
      title: "Project API Engineer",
      description: "Build the project API.",
      candidate_count: 0,
      chat_session_count: 0,
    },
    {
      ...readOnlyPosition(),
      id: "70000000-0000-4000-8000-000000000002",
      title: "Project QA Engineer",
      description: "Verify the complete project.",
      candidate_count: 0,
      chat_session_count: 0,
    },
  ];
}

function readOnlyCandidateSummary() {
  return {
    id: readOnlyCandidateId,
    source: "referral",
    source_ref: "read-only-candidate@example.test",
    is_favorite: false,
    viewed_at: null,
    created_at: fixtureTimestamp,
    best_score: 84,
    best_position_id: readOnlyPositionId,
    best_position_title: "Read-only Platform Engineer",
    score_count: 1,
  };
}

function readOnlyCandidateDetail() {
  return {
    id: readOnlyCandidateId,
    source: "referral",
    source_ref: "read-only-candidate@example.test",
    raw_cv: "Experienced platform engineer.",
    parsed_cv: {},
    pdf_path: null,
    has_original_file: false,
    github_login: null,
    github_data: null,
    github_fetched_at: null,
    is_favorite: false,
    viewed_at: null,
    created_at: fixtureTimestamp,
  };
}

function readOnlyCandidateApplication() {
  return {
    ...readOnlyPosition({ detail: true }).candidates[0],
    position_id: readOnlyPositionId,
    applied_at: fixtureTimestamp,
    cycle_number: 1,
    is_active: true,
  };
}

function assessmentApplication() {
  return {
    id: readOnlyScoreId,
    organization_id: interviewerOrganizationId,
    candidate_id: readOnlyCandidateId,
    position_id: readOnlyPositionId,
    legacy_score_id: readOnlyScoreId,
    stage: "screening",
    source: "referral",
    external_ref: null,
    owner_user_id: adminUserId,
    next_action_due_date: null,
    cycle_number: 1,
    is_active: true,
    metadata: {},
    applied_at: fixtureTimestamp,
    closed_at: null,
    archived_at: null,
    archived_by_user_id: null,
    created_at: fixtureTimestamp,
    updated_at: fixtureTimestamp,
    evaluation_count: 1,
    stage_events: [],
  };
}

function assessmentTemplate() {
  return {
    id: IDS.assessmentTemplate,
    name: "Knockout review fixture",
    description: "A deterministic reviewer-flow fixture.",
    status: "active",
    created_at: fixtureTimestamp,
    updated_at: fixtureTimestamp,
    versions: [
      {
        id: IDS.assessmentVersion,
        template_id: IDS.assessmentTemplate,
        version: 1,
        title: "Pilot screening",
        instructions: null,
        passing_percentage: "80.0000",
        time_limit_minutes: 30,
        published_at: fixtureTimestamp,
        sections: [],
      },
    ],
  };
}

function assessmentInvitation() {
  return {
    id: IDS.invitation,
    application_id: readOnlyScoreId,
    template_version_id: IDS.assessmentVersion,
    token_prefix: "asm_e2e_review",
    recipient_email: "candidate@example.test",
    status: "completed",
    expires_at: "2026-09-20T09:00:00.000Z",
    max_attempts: 1,
    authorized_attempts: 1,
    // Legacy invitations legitimately predate explicit accommodations. The
    // admin UI must render that nullable wire contract as the default policy.
    accommodations: null,
    first_opened_at: fixtureTimestamp,
    completed_at: fixtureTimestamp,
    created_at: fixtureTimestamp,
  };
}

function reviewerAssessmentSubmission(scope = "default") {
  const reviewState = assessmentReviewStates.get(scope) ?? null;
  const gradedAt = reviewState?.reviewed_at ?? fixtureTimestamp;
  const cleared = reviewState?.outcome === "cleared";
  return {
    attempt: {
      id: IDS.attempt,
      invitation_id: IDS.invitation,
      sequence: 1,
      status: "submitted",
      started_at: fixtureTimestamp,
      last_saved_at: fixtureTimestamp,
      expires_at: "2026-08-20T10:00:00.000Z",
      submitted_at: fixtureTimestamp,
      answers: [
        { question_id: IDS.consentQuestion, value: false, updated_at: fixtureTimestamp },
      ],
    },
    result: {
      id: IDS.assessmentResult,
      attempt_id: IDS.attempt,
      application_id: readOnlyScoreId,
      raw_score: "5.0000",
      max_score: "5.0000",
      percentage: "100.0000",
      passed: cleared,
      knocked_out: true,
      knockout_question_id: IDS.consentQuestion,
      knockout_reason: "Rule evidence retained for human review",
      knockout_review: reviewState,
      needs_manual_review: reviewState === null,
      breakdown: [
        {
          question_id: IDS.consentQuestion,
          key: "eligibility",
          visible: true,
          score: "5.0000",
          max_score: "5.0000",
          needs_manual_review: false,
        },
      ],
      graded_at: gradedAt,
    },
  };
}

function reviewerAssessmentAttemptSummary(scope = "default") {
  const submission = reviewerAssessmentSubmission(scope);
  return {
    ...submission.attempt,
    answers: undefined,
    result: {
      ...submission.result,
      attempt_id: undefined,
      application_id: undefined,
      breakdown: undefined,
    },
    files: [],
  };
}

function assessmentReviewScope(request, url) {
  const explicit = url.searchParams.get("scope");
  if (explicit && /^[A-Za-z0-9_-]{1,80}$/.test(explicit)) return explicit;
  const authorization = request.headers.authorization ?? "";
  const matched = authorization.match(
    /fixture-admin-access-token-scope-([A-Za-z0-9_-]{1,80})-/,
  );
  return matched?.[1] ?? "default";
}

function readOnlyCandidateSearchPage() {
  return {
    items: [
      {
        ...readOnlyCandidateSummary(),
        stages: ["interview"],
        tags: ["pilot"],
        applications: [
          {
            score_id: readOnlyScoreId,
            position_id: readOnlyPositionId,
            position_title: "Read-only Platform Engineer",
            stage: "interview",
            overall_score: 84,
          },
        ],
        duplicate_count: 0,
      },
    ],
    total: 1,
    available_sources: ["referral"],
    next_cursor: null,
  };
}

function readOnlyFolder(positionCount = 1) {
  return {
    id: readOnlyFolderId,
    organization_id: interviewerOrganizationId,
    parent_id: null,
    name: "Pilot Engineering",
    color: "blue",
    icon: "code",
    sort_order: 0,
    is_archived: false,
    position_count: positionCount,
    descendant_count: 0,
    created_at: fixtureTimestamp,
    updated_at: fixtureTimestamp,
    children: [],
  };
}

function interviewerPermissions() {
  return {
    role: "interviewer",
    permissions: {
      "account.read": true,
      "candidates.read": true,
      "candidates.write": false,
      "candidates.export": false,
      "candidates.contact": false,
      "applications.disposition": false,
      "communications.read": false,
      "communications.send": false,
      "ai.use": false,
      "ai.sensitive_context": false,
    },
  };
}

function adminPermissions() {
  return {
    role: "admin",
    permissions: {
      "enterprise.read": true,
      "enterprise.manage": true,
      "account.read": true,
      "applications.read": true,
      "candidates.read": true,
      "candidates.write": true,
      "requisitions.compensation": true,
      "ai.use": true,
      "ai.sensitive_context": true,
      "assessments.read": true,
      "assessments.manage": true,
      "assessments.grade": true,
      "assessments.results": true,
      "integrations.read": true,
      "integrations.manage": true,
      "integrations.credentials": true,
      "communications.read": true,
      "communications.manage": true,
      "interviews.read": true,
      "interviews.manage": true,
    },
  };
}

function sendJson(response, status, body, headers = {}) {
  response.writeHead(status, {
    "Cache-Control": "private, no-store",
    "Content-Type": "application/json; charset=utf-8",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    ...headers,
  });
  response.end(JSON.stringify(body));
}

function sendPng(request, response) {
  response.writeHead(200, {
    "Cache-Control": "public, max-age=300",
    "Content-Length": String(pixelPng.length),
    "Content-Type": "image/png",
    "X-Content-Type-Options": "nosniff",
  });
  response.end(request.method === "HEAD" ? undefined : pixelPng);
}

function problem(response, status, code, detail) {
  sendJson(
    response,
    status,
    { type: "about:blank", title: detail, status, code, detail },
    { "Content-Type": "application/problem+json; charset=utf-8" },
  );
}

function drain(request) {
  return new Promise((resolve, reject) => {
    request.on("data", () => {});
    request.on("end", resolve);
    request.on("error", reject);
  });
}

async function jsonBody(request) {
  const chunks = [];
  for await (const chunk of request) chunks.push(chunk);
  if (chunks.length === 0) return {};
  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
}

function validAssessmentToken(token) {
  return token.startsWith("asm_e2e_valid_");
}

function recordAssessmentEvent(token, event) {
  const events = assessmentEvents.get(token) ?? [];
  events.push({ ...event, received_at_ms: Date.now() });
  assessmentEvents.set(token, events);
}

function mergeAssessmentAnswers(token, incoming) {
  const current = attempts.get(token)?.answers ?? [];
  const merged = new Map(current.map((answer) => [answer.question_id, answer]));
  for (const answer of incoming) merged.set(answer.question_id, answer);
  return [...merged.values()];
}

function assessmentAttempt(token, answers = []) {
  return {
    id: IDS.attempt,
    invitation_id: IDS.invitation,
    sequence: 1,
    status: "in_progress",
    started_at: new Date().toISOString(),
    last_saved_at: new Date().toISOString(),
    expires_at: new Date(Date.now() + 30 * 60_000).toISOString(),
    submitted_at: null,
    answers,
    token,
  };
}

function assessmentLaunch(token) {
  const questions = [
    {
      id: IDS.consentQuestion,
      key: "consent",
      question_type: "boolean",
      prompt: "May the hiring team review these answers?",
      description: null,
      required: true,
      position: 0,
      options: [],
      validation: {},
      condition: {},
    },
    {
      id: IDS.noteQuestion,
      key: "note",
      question_type: "short",
      prompt: "What kind of work interests you?",
      description: "Optional; one sentence is enough.",
      required: false,
      position: 1,
      options: [],
      validation: { max_length: 200 },
      condition: {},
    },
  ];
  if (token.includes("numeric_conditions")) {
    questions.push(
      {
        id: IDS.numericQuestion,
        key: "years",
        question_type: "number",
        prompt: "How many years of relevant experience do you have?",
        description: null,
        required: true,
        position: 2,
        options: [],
        validation: { min_value: 0, max_value: 50 },
        condition: {},
      },
      {
        id: IDS.numericBranchQuestion,
        key: "two_year_example",
        question_type: "short",
        prompt: "Describe one achievement from those two years.",
        description: null,
        required: true,
        position: 3,
        options: [],
        validation: { max_length: 200 },
        condition: {
          mode: "any",
          rules: [
            { question_key: "years", operator: "equals", value: "2.0" },
            { question_key: "consent", operator: "equals", value: false },
          ],
        },
      },
    );
  }
  if (token.includes("all_types")) {
    questions.push(
      {
        id: IDS.singleQuestion,
        key: "track",
        question_type: "single",
        prompt: "Choose one track.",
        description: null,
        required: true,
        position: 2,
        options: [
          { value: "backend", label: "Backend" },
          { value: "frontend", label: "Frontend" },
        ],
        validation: {},
        condition: {},
      },
      {
        id: IDS.multiQuestion,
        key: "skills",
        question_type: "multi",
        prompt: "Choose your skills.",
        description: null,
        required: true,
        position: 3,
        options: [
          { value: "python", label: "Python" },
          { value: "sql", label: "SQL" },
        ],
        validation: {},
        condition: {},
      },
      {
        id: IDS.numericQuestion,
        key: "years",
        question_type: "number",
        prompt: "Years of experience.",
        description: null,
        required: true,
        position: 4,
        options: [],
        validation: { min_value: 0, max_value: 50 },
        condition: {},
      },
      {
        id: IDS.longQuestion,
        key: "summary",
        question_type: "long",
        prompt: "Describe your experience in detail.",
        description: null,
        required: true,
        position: 5,
        options: [],
        validation: { min_length: 10, max_length: 500 },
        condition: {},
      },
      {
        id: IDS.urlQuestion,
        key: "portfolio",
        question_type: "url",
        prompt: "Share a portfolio URL.",
        description: null,
        required: true,
        position: 6,
        options: [],
        validation: { min_length: 10, max_length: 2048 },
        condition: {},
      },
      {
        id: IDS.fileQuestion,
        key: "sample",
        question_type: "file",
        prompt: "Upload a work sample.",
        description: null,
        required: true,
        position: 7,
        options: [],
        validation: {
          allowed_file_extensions: ["txt"],
          max_file_size_bytes: 1048576,
        },
        condition: {},
      },
    );
  }
  return {
    invitation_id: IDS.invitation,
    title: "Pilot screening",
    instructions:
      "Answer the required questions. Your work is saved as you go.",
    expires_at: new Date(Date.now() + 24 * 60 * 60_000).toISOString(),
    time_limit_minutes: 30,
    max_attempts: 1,
    authorized_attempts: 1,
    attempts_started: attempts.has(token) ? 1 : 0,
    attempts_remaining: attempts.has(token) ? 0 : 1,
    can_start_attempt: !attempts.has(token),
    accommodations: token.includes("accommodations")
      ? {
          extra_time_minutes: 15,
          breaks_allowed: true,
          screen_reader_optimized: true,
        }
      : {
          extra_time_minutes: 0,
          breaks_allowed: false,
          screen_reader_optimized: false,
        },
    sections: [
      {
        id: IDS.section,
        title: "Core questions",
        description: "A short, deterministic pilot assessment.",
        position: 0,
        questions,
      },
    ],
    attempt: attempts.get(token) ?? null,
    result: null,
  };
}

function schedulingDetails() {
  return {
    organization_name: "Northstar Hiring",
    organization_logo_url: null,
    accent_color: "#2563eb",
    position_title: "Platform Engineer",
    duration_minutes: 45,
    location_mode: "video",
    expires_at: new Date(Date.now() + 7 * 24 * 60 * 60_000).toISOString(),
  };
}

function schedulingSlots(timezone) {
  const startsAt = new Date(Date.now() + 26 * 60 * 60_000);
  startsAt.setUTCMinutes(0, 0, 0);
  const second = new Date(startsAt.getTime() + 60 * 60_000);
  return {
    timezone,
    from_date: new Date().toISOString().slice(0, 10),
    through_date: new Date(Date.now() + 14 * 24 * 60 * 60_000)
      .toISOString()
      .slice(0, 10),
    items: [startsAt, second].map((start) => ({
      starts_at: start.toISOString(),
      ends_at: new Date(start.getTime() + 45 * 60_000).toISOString(),
      timezone,
    })),
  };
}

function publicAppointment(startsAt, timezone) {
  return {
    id: IDS.appointment,
    status: "scheduled",
    starts_at: startsAt,
    ends_at: new Date(new Date(startsAt).getTime() + 45 * 60_000).toISOString(),
    candidate_timezone: timezone,
    location_mode: "video",
    location_details: "A secure meeting link will be sent separately.",
    position_title: "Platform Engineer",
    provider: "local",
    sync_status: "local",
    version: 1,
    cancelled_at: null,
    interviewer_labels: ["Hiring team"],
  };
}

function publicAppointmentDetails() {
  const startsAt = new Date(Date.now() + 26 * 60 * 60_000);
  startsAt.setUTCMinutes(0, 0, 0);
  return {
    appointment: publicAppointment(startsAt.toISOString(), "UTC"),
    organization_name: "Northstar Hiring",
    organization_logo_url: null,
    accent_color: "#2563eb",
  };
}

function publicShortlist() {
  return {
    position_title: "Platform Engineer",
    position_description:
      "A private, evidence-based review shared with the hiring team.",
    position_company: "Northstar Hiring",
    organization_name: "Northstar Hiring",
    organization_logo_path: null,
    organization_accent_color: "#2563eb",
    employer_confidential: false,
    top_n: 1,
    include_contact: false,
    issued_at: fixtureTimestamp,
    candidates: [
      {
        candidate_id: readOnlyCandidateId,
        label: "Candidate #01",
        source: "referral",
        overall_score: 84,
        score_technical_skills: 88,
        score_quality_of_projects: 82,
        score_previous_experience: 83,
        score_impact: 81,
        comment_technical_skills: "Strong fundamentals.",
        comment_quality_of_projects: "Relevant delivery history.",
        comment_previous_experience: "Appropriate scope.",
        comment_impact: "Clear outcomes.",
        contact_name: null,
        contact_phone: null,
        contact_email: null,
      },
    ],
  };
}

function publicOffer(token) {
  return {
    organization_name: "Northstar Hiring",
    position_title: "Platform Engineer",
    subject: "Employment offer for Platform Engineer",
    body: "We are pleased to invite you to join the Northstar Hiring team.",
    salary_amount: "4200.00",
    salary_currency: "USD",
    start_date: "2026-09-15",
    expires_at: new Date(Date.now() + 7 * 24 * 60 * 60_000).toISOString(),
    status: offerResponses.get(token) ?? "sent",
  };
}

const server = createServer(async (request, response) => {
  try {
    const url = new URL(request.url ?? "/", `http://127.0.0.1:${port}`);
    const path = url.pathname;

    if (request.method === "GET" && path === "/__health") {
      return sendJson(response, 200, { ok: true });
    }

    if (path === "/__state/enterprise-policy") {
      if (request.method === "GET") {
        return sendJson(response, 200, enterprisePolicy);
      }
      if (request.method === "POST") {
        enterprisePolicy = defaultEnterprisePolicy();
        await drain(request);
        return sendJson(response, 200, enterprisePolicy);
      }
    }

    if (path === "/__state/assessment-review") {
      const scope = assessmentReviewScope(request, url);
      if (request.method === "GET") {
        return sendJson(response, 200, {
          review: assessmentReviewStates.get(scope) ?? null,
          requests: assessmentReviewRequests.get(scope) ?? [],
        });
      }
      if (request.method === "POST") {
        assessmentReviewStates.delete(scope);
        assessmentReviewRequests.delete(scope);
        await drain(request);
        return sendJson(response, 200, { review: null, requests: [] });
      }
    }

    if (request.method === "POST" && path === "/auth/login") {
      const body = await jsonBody(request);
      const isInterviewer = body.email === "interviewer@example.test";
      const isRefreshReplay = body.email === "refresh-replay@example.test";
      const isLogoutRefreshOutage =
        body.email === "admin+logout-refresh-outage@example.test";
      const isLegacyOrganization =
        body.email === "admin+legacy-org@example.test";
      const draftIdentity =
        body.email === "draft-a@example.test"
          ? {
              token: "draft-a",
              organization: {
                id: draftOrganizationAId,
                name: "Draft Tenant A",
                slug: "draft-tenant-a",
              },
              userId: draftUserAId,
            }
          : body.email === "draft-b@example.test"
            ? {
                token: "draft-b",
                organization: {
                  id: draftOrganizationBId,
                  name: "Draft Tenant B",
                  slug: "draft-tenant-b",
                },
                userId: draftUserBId,
              }
            : null;
      const scopedAdmin = /^admin\+([A-Za-z0-9_-]{1,80})@example\.test$/.exec(
        body.email,
      );
      const isAdmin =
        body.email === "admin@example.test" ||
        scopedAdmin !== null ||
        draftIdentity !== null ||
        isRefreshReplay;
      if ((!isInterviewer && !isAdmin) || body.password !== "fixture-password") {
        return problem(
          response,
          401,
          "invalid_credentials",
          "Invalid credentials",
        );
      }
      return sendJson(response, 200, {
        access_token: isAdmin
          ? isRefreshReplay
            ? `fixture-refresh-replay-access-${"x".repeat(40)}`
            : isLogoutRefreshOutage
              ? `fixture-admin-access-token-scope-logout-failure-${"x".repeat(40)}`
            : draftIdentity
            ? `fixture-admin-access-token-${draftIdentity.token}-${"x".repeat(40)}`
            : scopedAdmin
            ? `fixture-admin-access-token-scope-${scopedAdmin[1]}-${"x".repeat(40)}`
            : "fixture-admin-access-token-".repeat(2)
          : "fixture-access-token-".repeat(2),
        challenge_token: null,
        expires_in: isRefreshReplay || isLogoutRefreshOutage ? 46 : 3600,
        locale: "en",
        mfa_required: false,
        mfa_setup_required: false,
        organization: draftIdentity?.organization ?? {
          id: isLegacyOrganization
            ? "00000000-0000-0000-0000-000000000001"
            : interviewerOrganizationId,
          name: "Northstar Hiring",
          slug: "northstar-hiring",
        },
        refresh_token: isRefreshReplay
          ? `fixture-refresh-replay-old-${"r".repeat(40)}`
          : isLogoutRefreshOutage
            ? `fixture-refresh-outage-${"o".repeat(40)}`
          : "fixture-refresh-token-".repeat(2),
        role: isAdmin ? "admin" : "interviewer",
        token_type: "bearer",
        user_id: draftIdentity?.userId ?? (isAdmin ? adminUserId : interviewerUserId),
      });
    }

    if (request.method === "POST" && path === "/auth/refresh") {
      const body = await jsonBody(request);
      if (body.refresh_token === `fixture-refresh-outage-${"o".repeat(40)}`) {
        return problem(
          response,
          503,
          "auth_service_unavailable",
          "Refresh service unavailable",
        );
      }
      if (
        body.refresh_token === `fixture-refresh-replay-old-${"r".repeat(40)}`
      ) {
        replayRefreshAttempts += 1;
        if (replayRefreshAttempts > 1) {
          return problem(
            response,
            401,
            "refresh_token_replayed",
            "Refresh token has already been rotated",
          );
        }
        await new Promise((resolve) => setTimeout(resolve, 150));
        return sendJson(response, 200, {
          access_token: `fixture-refresh-replay-new-access-${"y".repeat(40)}`,
          challenge_token: null,
          expires_in: 3600,
          locale: "en",
          mfa_required: false,
          mfa_setup_required: false,
          organization: {
            id: interviewerOrganizationId,
            name: "Northstar Hiring",
            slug: "northstar-hiring",
          },
          refresh_token: `fixture-refresh-replay-new-${"s".repeat(40)}`,
          role: "admin",
          token_type: "bearer",
          user_id: adminUserId,
        });
      }
      return problem(
        response,
        401,
        "invalid_refresh_token",
        "Refresh token is invalid",
      );
    }

    if (request.method === "GET" && path === "/__state/refresh-attempts") {
      return sendJson(response, 200, { attempts: replayRefreshAttempts });
    }

    if (request.method === "POST" && path === "/__state/refresh-attempts") {
      replayRefreshAttempts = 0;
      await drain(request);
      return sendJson(response, 200, { attempts: 0 });
    }

    if (request.method === "GET" && path === "/__state/logout-requests") {
      return sendJson(response, 200, { paths: logoutRequests });
    }

    if (request.method === "POST" && path === "/__state/logout-requests") {
      logoutRequests = [];
      await drain(request);
      return sendJson(response, 200, { paths: [] });
    }

    if (
      request.method === "POST" &&
      (path === "/auth/logout" || path === "/auth/logout-all")
    ) {
      await drain(request);
      logoutRequests.push(path);
      if (
        request.headers.authorization?.includes("scope-logout-failure-")
      ) {
        return problem(
          response,
          503,
          "auth_service_unavailable",
          "Revocation service unavailable",
        );
      }
      response.writeHead(204, { "Cache-Control": "private, no-store" });
      response.end();
      return;
    }

    if (
      request.method === "POST" &&
      path === `/ai/v1/sessions/${AI_SESSION_OK}/messages`
    ) {
      await drain(request);
      response.writeHead(200, {
        "Cache-Control": "public, max-age=86400",
        "Content-Type": "text/event-stream; charset=utf-8",
        "Referrer-Policy": "unsafe-url",
        "X-Content-Type-Options": "invalid-upstream-value",
      });
      response.end(
        `event: done\ndata: ${JSON.stringify({ type: "done", session_id: AI_SESSION_OK })}\n\n`,
      );
      return;
    }

    if (
      request.method === "POST" &&
      path === `/ai/v1/sessions/${AI_SESSION_OK}/attachments`
    ) {
      await drain(request);
      return sendJson(
        response,
        200,
        {
          id: "80000000-0000-4000-8000-000000000003",
          filename: "private-cv.txt",
          media_type: "text/plain",
          size_bytes: 20,
          status: "uploaded",
          sha256: "a".repeat(64),
          upload_token: "one-time-private-upload-token",
        },
        {
          "Cache-Control": "public, max-age=86400",
          "Referrer-Policy": "unsafe-url",
          "X-Content-Type-Options": "invalid-upstream-value",
        },
      );
    }

    if (
      request.method === "POST" &&
      (path === `/ai/v1/sessions/${AI_SESSION_ERROR}/messages` ||
        path === `/ai/v1/sessions/${AI_SESSION_ERROR}/attachments`)
    ) {
      await drain(request);
      return sendJson(
        response,
        502,
        { detail: AI_UPSTREAM_SECRET, prompt: "CV and salary contents" },
        { "Cache-Control": "public, max-age=86400" },
      );
    }

    if (request.method === "GET" && path === "/enterprise/permissions/me") {
      return sendJson(
        response,
        200,
        request.headers.authorization?.includes("fixture-admin-access-token")
          ? adminPermissions()
          : interviewerPermissions(),
      );
    }

    if (
      request.method === "GET" &&
      /^\/positions\/[0-9a-f-]+\/rescore-capability$/.test(path)
    ) {
      if (
        request.headers.authorization?.includes("scope-ai-unavailable-")
      ) {
        return sendJson(response, 200, {
          available: false,
          route: null,
          reason: "ai_managed_processing_opt_in_required",
        });
      }
      return sendJson(response, 200, {
        available: true,
        route: "managed",
        reason: null,
      });
    }

    if (request.method === "GET" && path === "/chat/capability") {
      if (
        request.headers.authorization?.includes("scope-chat-denied-")
      ) {
        return problem(
          response,
          403,
          "forbidden",
          "Sensitive field access denied",
        );
      }
      if (
        request.headers.authorization?.includes("scope-ai-unavailable-")
      ) {
        return sendJson(response, 200, {
          available: false,
          route: null,
          reason: "ai_managed_processing_opt_in_required",
        });
      }
      return sendJson(response, 200, {
        available: true,
        route: "managed",
        reason: null,
      });
    }

    if (request.method === "GET" && path === "/chat/sessions") {
      return sendJson(response, 200, { items: [], next_cursor: null });
    }

    if (request.method === "GET" && path === "/users") {
      return sendJson(response, 200, {
        items: [
          {
            id: adminUserId,
            email: "admin@example.test",
            role: "admin",
            locale: "en",
            is_active: true,
            mfa_enabled: false,
            organization_id: interviewerOrganizationId,
            created_at: fixtureTimestamp,
          },
          {
            id: recruiterUserId,
            email: "recruiter@example.test",
            role: "recruiter",
            locale: "en",
            is_active: true,
            mfa_enabled: false,
            organization_id: interviewerOrganizationId,
            created_at: fixtureTimestamp,
          },
        ],
        next_cursor: null,
      });
    }

    if (request.method === "GET" && path === "/communications/accounts") {
      return sendJson(response, 200, { items: [], next_cursor: null });
    }

    if (request.method === "GET" && path === "/communications/templates") {
      return sendJson(response, 200, {
        items: [
          {
            id: legacyWhatsAppTemplateId,
            name: "Legacy WhatsApp reminder",
            channel: "whatsapp",
            language: "en",
            subject_template: null,
            body_template: "Hello {candidate.first_name}, your interview is tomorrow.",
            is_active: true,
            created_at: fixtureTimestamp,
            updated_at: fixtureTimestamp,
          },
        ],
        next_cursor: null,
      });
    }

    if (request.method === "GET" && path === "/scorecard-templates") {
      if (
        request.headers.authorization?.includes(
          "scope-scorecard-load-failure-",
        )
      ) {
        return problem(
          response,
          503,
          "scorecard_templates_unavailable",
          "Scorecard templates unavailable",
        );
      }
      return sendJson(response, 200, { items: [], next_cursor: null });
    }

    if (request.method === "GET" && path === "/integrations") {
      return sendJson(response, 200, { items: [], next_cursor: null });
    }

    if (
      request.method === "POST" &&
      path === "/integrations/oauth/callback"
    ) {
      await jsonBody(request);
      return problem(
        response,
        400,
        "oauth_provider_unknown_internal_code",
        "Unknown provider callback failure",
      );
    }

    if (path === "/enterprise/policy") {
      if (request.method === "GET") {
        return sendJson(response, 200, enterprisePolicy);
      }
      if (request.method === "PATCH") {
        const body = await jsonBody(request);
        const separation = body.approval_separation_of_duties;
        if (
          !separation ||
          typeof separation !== "object" ||
          Array.isArray(separation) ||
          Object.keys(separation).sort().join(",") !==
            "hiring_decisions,offers,requisitions" ||
          Object.values(separation).some((value) => typeof value !== "boolean")
        ) {
          return problem(
            response,
            422,
            "invalid_policy",
            "Approval separation policy must be complete",
          );
        }
        enterprisePolicy = {
          ...enterprisePolicy,
          ...body,
          approval_separation_of_duties: { ...separation },
          updated_at: new Date().toISOString(),
        };
        return sendJson(response, 200, enterprisePolicy);
      }
    }

    if (request.method === "GET" && path === "/my-work") {
      return sendJson(response, 200, {
        items: [],
        counts: {
          all: 0,
          overdue: 0,
          today: 0,
          upcoming: 0,
          undated: 0,
          interviews_today: 0,
          stale: 0,
          missing_feedback: 0,
        },
        today: "2026-08-22",
        next_cursor: null,
      });
    }

    if (request.method === "GET" && path === "/calendar/appointments/me") {
      return sendJson(response, 200, { items: [], next_cursor: null });
    }

    if (request.method === "GET" && path === "/notifications") {
      return sendJson(response, 200, {
        items: [],
        unread_count: 0,
        next_cursor: null,
      });
    }

    if (request.method === "GET" && path === "/candidate-search") {
      return sendJson(response, 200, readOnlyCandidateSearchPage());
    }

    if (request.method === "GET" && path === "/candidate-views") {
      return sendJson(response, 200, { items: [], next_cursor: null });
    }

    if (request.method === "GET" && path === "/candidate-duplicates") {
      return sendJson(response, 200, {
        items: [],
        candidate_count: 0,
        next_cursor: null,
      });
    }

    if (request.method === "GET" && path === "/folders") {
      const hasProject = projectCreationTokens.has(
        request.headers.authorization ?? "",
      );
      return sendJson(response, 200, {
        items: [readOnlyFolder(hasProject ? 2 : 1)],
        next_cursor: null,
      });
    }

    if (request.method === "POST" && path === "/positions/project/extract") {
      await drain(request);
      return sendJson(response, 200, {
        project_name: "Project Phoenix",
        folder: "Pilot Engineering",
        positions: [
          {
            title: "Project API Engineer",
            description: "Build the project API.",
            requirements: { skills: ["FastAPI", "PostgreSQL"] },
            seniority: "senior",
            work_format: "hybrid",
            location: "Dushanbe",
            employment_type: "full-time",
            salary_range: null,
            team_size: null,
            folder: null,
          },
          {
            title: "Project QA Engineer",
            description: "Verify the complete project.",
            requirements: { skills: ["Playwright", "security testing"] },
            seniority: "middle",
            work_format: "hybrid",
            location: "Dushanbe",
            employment_type: "full-time",
            salary_range: null,
            team_size: null,
            folder: null,
          },
        ],
        source_filename: "project-brief.txt",
        source_chars: 128,
        model_used: "fixture-private-model",
      });
    }

    if (request.method === "POST" && path === "/positions/project") {
      await jsonBody(request);
      projectCreationTokens.add(request.headers.authorization ?? "");
      return sendJson(response, 201, {
        created: projectPositions().map((position) => position.id),
        folder_id: readOnlyFolderId,
        folder: "Pilot Engineering",
      });
    }

    if (request.method === "GET" && path === "/positions") {
      const hasProject = projectCreationTokens.has(
        request.headers.authorization ?? "",
      );
      return sendJson(response, 200, {
        items: hasProject ? projectPositions() : [readOnlyPosition()],
        next_cursor: null,
      });
    }

    if (
      request.method === "GET" &&
      path === `/positions/${readOnlyPositionId}`
    ) {
      return sendJson(response, 200, readOnlyPosition({ detail: true }));
    }

    if (
      request.method === "GET" &&
      path === `/positions/${readOnlyPositionId}/applications`
    ) {
      return sendJson(response, 200, {
        items: readOnlyPosition({ detail: true }).candidates,
        next_cursor: null,
      });
    }

    if (request.method === "GET" && path === "/candidates") {
      return sendJson(response, 200, {
        items: [readOnlyCandidateSummary()],
        next_cursor: null,
      });
    }

    if (
      request.method === "GET" &&
      path === `/candidates/${readOnlyCandidateId}`
    ) {
      return sendJson(response, 200, readOnlyCandidateDetail());
    }

    if (
      request.method === "GET" &&
      path === `/candidates/${readOnlyCandidateId}/applications`
    ) {
      return sendJson(response, 200, {
        items: [readOnlyCandidateApplication()],
        next_cursor: null,
      });
    }

    if (request.method === "GET" && path === "/applications") {
      return sendJson(response, 200, {
        items: [assessmentApplication()],
        next_cursor: null,
      });
    }

    if (request.method === "GET" && path === "/assessment-templates") {
      return sendJson(response, 200, {
        items: [assessmentTemplate()],
        next_cursor: null,
      });
    }

    if (
      request.method === "GET" &&
      path === `/assessment-templates/${IDS.assessmentTemplate}`
    ) {
      return sendJson(response, 200, assessmentTemplate());
    }

    if (
      request.method === "GET" &&
      path === `/applications/${readOnlyScoreId}/assessment-invitations`
    ) {
      return sendJson(response, 200, {
        items: [assessmentInvitation()],
        next_cursor: null,
      });
    }

    if (
      request.method === "GET" &&
      path === `/assessment-invitations/${IDS.invitation}/attempts`
    ) {
      const scope = assessmentReviewScope(request, url);
      return sendJson(response, 200, {
        items: [reviewerAssessmentAttemptSummary(scope)],
        next_cursor: null,
      });
    }

    if (
      request.method === "GET" &&
      path === `/assessment-attempts/${IDS.attempt}`
    ) {
      return sendJson(
        response,
        200,
        reviewerAssessmentSubmission(assessmentReviewScope(request, url)),
      );
    }

    if (
      request.method === "POST" &&
      path === `/assessment-attempts/${IDS.attempt}/manual-grade`
    ) {
      const scope = assessmentReviewScope(request, url);
      const body = await jsonBody(request);
      const review = body.knockout_review;
      if (
        !Array.isArray(body.grades) ||
        body.grades.length !== 0 ||
        !review ||
        !new Set(["confirmed", "cleared"]).has(review.outcome) ||
        typeof review.reason !== "string" ||
        !review.reason.trim()
      ) {
        return problem(
          response,
          422,
          "invalid_assessment_review",
          "A bounded knockout review is required",
        );
      }
      const requests = assessmentReviewRequests.get(scope) ?? [];
      requests.push(body);
      assessmentReviewRequests.set(scope, requests);
      assessmentReviewStates.set(scope, {
        outcome: review.outcome,
        reason: review.reason.trim(),
        reviewed_by_user_id: adminUserId,
        reviewed_at: new Date(Date.now() + requests.length).toISOString(),
      });
      return sendJson(response, 200, reviewerAssessmentSubmission(scope));
    }

    if (
      request.method === "GET" &&
      path === "/integrations/assessment/connections"
    ) {
      return sendJson(response, 200, { items: [], next_cursor: null });
    }

    if (
      request.method === "GET" &&
      path === `/integrations/assessment/applications/${readOnlyScoreId}/evidence`
    ) {
      return sendJson(response, 200, {
        application_id: readOnlyScoreId,
        items: [],
      });
    }

    const assessmentStateMatch = path.match(
      /^\/__state\/assessments\/([^/]+)$/,
    );
    if (request.method === "GET" && assessmentStateMatch) {
      const token = decodeURIComponent(assessmentStateMatch[1]);
      return sendJson(response, 200, {
        events: assessmentEvents.get(token) ?? [],
        answers: attempts.get(token)?.answers ?? [],
      });
    }

    if (
      (request.method === "GET" || request.method === "HEAD") &&
      path ===
        `/storage/logos/${brandingOrganizationId}/${brandingVersionId}.png`
    ) {
      return sendPng(request, response);
    }

    if (
      request.method === "HEAD" &&
      new Set([
        "/public/positions/e2e-relative-logo",
        "/public/positions/e2e-cdn-logo",
        "/public/positions/e2e-confidential",
        "/public/positions/e2e-position",
      ]).has(path)
    ) {
      response.writeHead(204, {
        "cache-control": "private, no-store, max-age=0",
      });
      return response.end();
    }

    if (request.method === "HEAD" && path.startsWith("/public/positions/")) {
      response.writeHead(404, {
        "cache-control": "private, no-store, max-age=0",
        "content-type": "application/problem+json",
      });
      return response.end();
    }

    if (
      request.method === "GET" &&
      path === "/public/positions/e2e-relative-logo"
    ) {
      return sendJson(response, 200, {
        id: "e2e-relative-logo",
        title: "Relative Logo Role",
        description: "Branding proxy fixture.",
        full_description: null,
        requirements: { company: "Northstar Hiring" },
        organization_name: "Northstar Hiring",
        organization_logo_path: `/storage/logos/${brandingOrganizationId}/${brandingVersionId}.png`,
        organization_accent_color: "#2563eb",
        employer_confidential: false,
      });
    }

    if (request.method === "GET" && path === "/public/positions/e2e-cdn-logo") {
      return sendJson(response, 200, {
        id: "e2e-cdn-logo",
        title: "CDN Logo Role",
        description: "Direct HTTPS branding fixture.",
        full_description: null,
        requirements: { company: "Northstar CDN" },
        organization_name: "Northstar CDN",
        organization_logo_path: "https://cdn.example.test/brand.png",
        organization_accent_color: "#2563eb",
        employer_confidential: false,
      });
    }

    if (
      request.method === "GET" &&
      path === "/public/positions/e2e-confidential"
    ) {
      return sendJson(response, 200, {
        id: "e2e-confidential",
        title: "Confidential Role",
        description: "Employer details are withheld.",
        full_description: null,
        requirements: {},
        organization_name: null,
        organization_logo_path: `/storage/logos/${brandingOrganizationId}/${brandingVersionId}.png`,
        organization_accent_color: null,
        employer_confidential: true,
      });
    }

    if (request.method === "GET" && path === "/public/positions/e2e-position") {
      return sendJson(response, 200, {
        id: "e2e-position",
        title: "Platform Engineer",
        description: "Build reliable hiring workflows for regional teams.",
        full_description:
          "Own resilient services, accessible interfaces, and safe integrations.",
        requirements: {
          company: "Northstar Hiring",
          skills: ["TypeScript", "Python"],
          experience_years: 3,
        },
        organization_name: "Northstar Hiring",
        organization_logo_path: null,
        organization_accent_color: "#2563eb",
        employer_confidential: false,
      });
    }

    if (
      request.method === "POST" &&
      path === "/public/positions/e2e-position/apply"
    ) {
      await drain(request);
      return sendJson(response, 202, {
        accepted: true,
        candidate_id: "30000000-0000-4000-8000-000000000001",
      });
    }

    if (request.method === "GET" && path.startsWith("/public/positions/")) {
      return problem(response, 404, "position_not_found", "Position not found");
    }

    if (request.method === "GET" && path.startsWith("/public/referrals/")) {
      return problem(response, 404, "referral_not_found", "Referral not found");
    }

    const shortlistMatch = path.match(/^\/public\/shortlists\/([^/]+)$/);
    if (request.method === "GET" && shortlistMatch) {
      const token = decodeURIComponent(shortlistMatch[1]);
      if (token.startsWith("e2e-shortlist-valid-")) {
        return sendJson(response, 200, publicShortlist());
      }
      if (token.startsWith("e2e-shortlist-expired-")) {
        return problem(
          response,
          410,
          "shortlist_expired",
          "Shortlist expired",
        );
      }
      if (token.startsWith("e2e-shortlist-revoked-")) {
        return problem(
          response,
          410,
          "shortlist_revoked",
          "Shortlist revoked",
        );
      }
      if (token.startsWith("e2e-shortlist-outage-")) {
        return problem(
          response,
          503,
          "service_unavailable",
          "Shortlist service unavailable",
        );
      }
      return problem(response, 404, "shortlist_not_found", "Shortlist not found");
    }

    const offerMatch = path.match(
      /^\/public\/offers\/([^/]+)(?:\/(respond))?$/,
    );
    if (offerMatch) {
      const token = decodeURIComponent(offerMatch[1]);
      const action = offerMatch[2];
      if (token.startsWith("e2e-offer-expired-")) {
        return problem(response, 410, "offer_expired", "Offer expired");
      }
      if (token.startsWith("e2e-offer-outage-")) {
        return problem(
          response,
          503,
          "service_unavailable",
          "Offer service unavailable",
        );
      }
      if (token.startsWith("e2e-offer-redirect-")) {
        response.writeHead(302, {
          Location: "https://attacker.invalid/collect",
        });
        return response.end();
      }
      if (!token.startsWith("e2e-offer-valid-")) {
        return problem(response, 404, "offer_not_found", "Offer not found");
      }
      if (request.method === "GET" && !action) {
        return sendJson(response, 200, publicOffer(token));
      }
      if (request.method === "POST" && action === "respond") {
        const body = await jsonBody(request);
        if (body.response !== "accept" && body.response !== "decline") {
          return problem(response, 422, "invalid_response", "Invalid response");
        }
        offerResponses.set(
          token,
          body.response === "accept" ? "accepted" : "declined",
        );
        return sendJson(response, 200, publicOffer(token));
      }
    }

    if (
      request.method === "GET" &&
      /^\/auth\/(invitations|password-reset)\/e2e_expired_[A-Za-z0-9_-]+$/.test(
        path,
      )
    ) {
      return problem(
        response,
        410,
        "access_token_gone",
        "Private access link is gone",
      );
    }

    const assessmentFileUploadMatch = path.match(
      /^\/public\/assessments\/([^/]+)\/attempts\/([^/]+)\/questions\/([^/]+)\/files$/,
    );
    if (assessmentFileUploadMatch && request.method === "POST") {
      const token = decodeURIComponent(assessmentFileUploadMatch[1]);
      const attemptId = assessmentFileUploadMatch[2];
      const questionId = assessmentFileUploadMatch[3];
      await drain(request);
      if (
        !validAssessmentToken(token) ||
        attemptId !== IDS.attempt ||
        questionId !== IDS.fileQuestion
      ) {
        return problem(response, 404, "assessment_file_not_found", "Assessment file not found");
      }
      return sendJson(response, 201, {
        file_id: IDS.assessmentFile,
        question_id: IDS.fileQuestion,
        filename: "proof.txt",
        media_type: "text/plain",
        size_bytes: 21,
        sha256: "0".repeat(64),
        status: "ready",
      });
    }

    const assessmentFileDeleteMatch = path.match(
      /^\/public\/assessments\/([^/]+)\/attempts\/([^/]+)\/files\/([^/]+)$/,
    );
    if (assessmentFileDeleteMatch && request.method === "DELETE") {
      const token = decodeURIComponent(assessmentFileDeleteMatch[1]);
      if (
        !validAssessmentToken(token) ||
        assessmentFileDeleteMatch[2] !== IDS.attempt ||
        assessmentFileDeleteMatch[3] !== IDS.assessmentFile
      ) {
        return problem(response, 404, "assessment_file_not_found", "Assessment file not found");
      }
      response.writeHead(204);
      return response.end();
    }

    const assessmentMatch = path.match(
      /^\/public\/assessments\/([^/]+)(?:\/attempts(?:\/([^/]+)(?:\/(answers|submit))?)?)?$/,
    );
    if (assessmentMatch) {
      const token = decodeURIComponent(assessmentMatch[1]);
      const attemptId = assessmentMatch[2];
      const action = assessmentMatch[3];
      if (token.startsWith("asm_e2e_expired_")) {
        return problem(
          response,
          410,
          "assessment_expired",
          "Assessment expired",
        );
      }
      if (!validAssessmentToken(token)) {
        return problem(
          response,
          404,
          "assessment_not_found",
          "Assessment not found",
        );
      }
      if (
        request.method === "GET" &&
        !attemptId &&
        attempts.get(token)?.status === "submitted"
      ) {
        return problem(
          response,
          410,
          "assessment_consumed",
          "Assessment already submitted",
        );
      }
      if (request.method === "GET" && !attemptId) {
        return sendJson(response, 200, assessmentLaunch(token));
      }
      if (request.method === "POST" && !attemptId) {
        const attempt = attempts.get(token) ?? assessmentAttempt(token);
        attempts.set(token, attempt);
        return sendJson(response, 201, attempt);
      }
      if (attemptId !== IDS.attempt) {
        return problem(response, 404, "attempt_not_found", "Attempt not found");
      }
      if (request.method === "PATCH" && action === "answers") {
        const body = await jsonBody(request);
        const incoming = Array.isArray(body.answers)
          ? body.answers.map((answer) => ({
              ...answer,
              updated_at: new Date().toISOString(),
            }))
          : [];
        recordAssessmentEvent(token, {
          type: "patch",
          answers: incoming.map(({ question_id, value }) => ({
            question_id,
            value,
          })),
        });
        const answers = mergeAssessmentAnswers(token, incoming);
        const attempt = assessmentAttempt(token, answers);
        attempts.set(token, attempt);
        return sendJson(response, 200, attempt);
      }
      if (request.method === "POST" && action === "submit") {
        recordAssessmentEvent(token, { type: "submit" });
        const needsManualReview = token.includes("manual_review");
        const submittedAt = new Date().toISOString();
        const attempt = {
          ...(attempts.get(token) ?? assessmentAttempt(token)),
          status: "submitted",
          submitted_at: submittedAt,
        };
        attempts.set(token, attempt);
        return sendJson(response, 200, {
          attempt,
          result: {
            attempt_id: IDS.attempt,
            submitted_at: submittedAt,
            percentage: needsManualReview ? "0.00" : "100.00",
            passed: !needsManualReview,
            needs_manual_review: needsManualReview,
          },
        });
      }
    }

    const schedulingManageMatch = path.match(
      /^\/public\/scheduling\/manage\/([^/]+)(?:\/(slots|reschedule|cancel))?$/,
    );
    if (schedulingManageMatch) {
      const token = decodeURIComponent(schedulingManageMatch[1]);
      const action = schedulingManageMatch[2];
      if (token.startsWith("e2e-manage-expired-")) {
        return problem(
          response,
          410,
          "link_expired",
          "Appointment management link expired",
        );
      }
      if (token.startsWith("e2e-manage-revoked-")) {
        return problem(
          response,
          410,
          "link_revoked",
          "Appointment management link revoked",
        );
      }
      if (token.startsWith("e2e-manage-outage-")) {
        return problem(
          response,
          503,
          "service_unavailable",
          "Scheduling service unavailable",
        );
      }
      if (token !== "e2e-manage") {
        return problem(
          response,
          404,
          "appointment_not_found",
          "Appointment not found",
        );
      }
      if (request.method === "GET" && !action) {
        return sendJson(response, 200, publicAppointmentDetails());
      }
      if (request.method === "GET" && action === "slots") {
        return sendJson(
          response,
          200,
          schedulingSlots(url.searchParams.get("timezone") ?? "UTC"),
        );
      }
    }

    const schedulingMatch = path.match(
      /^\/public\/scheduling\/([^/]+)(?:\/(slots|book))?$/,
    );
    if (schedulingMatch) {
      const token = decodeURIComponent(schedulingMatch[1]);
      const action = schedulingMatch[2];
      if (token.startsWith("e2e-schedule-expired-")) {
        return problem(
          response,
          410,
          "link_expired",
          "Scheduling link expired",
        );
      }
      if (token.startsWith("e2e-schedule-revoked-")) {
        return problem(
          response,
          410,
          "link_revoked",
          "Scheduling link revoked",
        );
      }
      if (token.startsWith("e2e-schedule-consumed-")) {
        return problem(
          response,
          409,
          "link_booked",
          "Scheduling link already used",
        );
      }
      if (token.startsWith("e2e-schedule-outage-")) {
        return problem(
          response,
          503,
          "service_unavailable",
          "Scheduling service unavailable",
        );
      }
      if (token !== "e2e-schedule") {
        return problem(
          response,
          404,
          "link_not_found",
          "Scheduling link not found",
        );
      }
      if (request.method === "GET" && !action) {
        return sendJson(response, 200, schedulingDetails());
      }
      if (request.method === "GET" && action === "slots") {
        return sendJson(
          response,
          200,
          schedulingSlots(url.searchParams.get("timezone") ?? "UTC"),
        );
      }
      if (request.method === "POST" && action === "book") {
        const body = await jsonBody(request);
        return sendJson(response, 201, {
          appointment: publicAppointment(body.starts_at, body.timezone),
          manage_url: "/public/scheduling/manage/e2e-manage",
        });
      }
    }

    return problem(response, 404, "not_found", "Fixture route not found");
  } catch {
    return problem(response, 500, "fixture_error", "Fixture request failed");
  }
});

server.listen(port, "127.0.0.1", () => {
  process.stdout.write(`mock-api listening on http://127.0.0.1:${port}\n`);
});

function close() {
  server.close(() => process.exit(0));
}

process.on("SIGINT", close);
process.on("SIGTERM", close);
