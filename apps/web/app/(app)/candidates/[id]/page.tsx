import Link from "next/link";
import { notFound, unstable_rethrow } from "next/navigation";
import { AlertTriangle, Download, ExternalLink, GitBranch } from "lucide-react";

import { auth } from "@/auth";
import { apiGet, apiGetAll } from "@/lib/api-client";
import { candRef, prjRef } from "@/lib/format";
import type { ApiResponseSchema } from "@/lib/generated/contracts";
import { getMessages } from "@/lib/i18n";
import { SCORE_VAR, scoreTier } from "@/lib/score";
import {
  normalizeCandidateDetail,
  normalizeInterviewRecording,
  normalizeTaskSubmission,
} from "@/lib/types";
import type {
  AssignableUser,
  CandidateDetail,
  CandidateApplicationPage,
  CandidateActivityPage,
  CandidateContactPoint,
  CandidateMergeHistoryPage,
  CandidateMergedSource,
  CandidateNote,
  CandidateTag,
  CommunicationAccount,
  CommunicationTemplate,
  CommunicationThread,
  CandidateHandoff,
  CandidateOffer,
  EffectivePermissions,
  EnterpriseFieldVisibility,
  InterviewAppointment,
  Interview,
  InterviewRecording,
  Position,
  PositionMatch,
  OfferTemplate,
  Score,
  SchedulingLink,
  ScorecardPanel,
  ScorecardTemplate,
  TaskSubmission,
} from "@/lib/types";
import type { Messages as I18nMessages } from "@/lib/i18n/types";
import { CollapsibleChat } from "@/components/chat/collapsible-chat";
import { TabFocusRefresh } from "@/components/app/tab-focus-refresh";
import { Topbar } from "@/components/app/topbar";
import { DeleteCandidateButton } from "@/components/candidates/delete-candidate-button";
import { BestPositionsSection } from "@/components/candidates/best-positions-section";
import { CriterionCard } from "@/components/candidates/criterion-card";
import { FavoriteButton } from "@/components/candidates/favorite-button";
import { GitHubSection } from "@/components/candidates/github-section";
import { InterviewSection } from "@/components/candidates/interview-section";
import { MarkViewedOnMount } from "@/components/candidates/mark-viewed-on-mount";
import { MergedSourcesSection } from "@/components/candidates/merged-sources-section";
import { NotesSection } from "@/components/candidates/notes-section";
import { RecordingsSection } from "@/components/candidates/recordings-section";
import { TagsRow } from "@/components/candidates/tags-row";
import { TaskSubmissionSection } from "@/components/candidates/task-submission-section";
import { ActivityTimeline } from "@/components/candidates/activity-timeline";
import { Collapsible } from "@/components/ui/collapsible";
import { JsonView } from "@/components/ui/json-view";
import { SourceBadge } from "@/components/ui/source-badge";
import { StatusBadge } from "@/components/ui/status-badge";
import { WorkControls } from "@/components/work/work-controls";
import { CandidateCommunicationsPanel } from "@/components/communications/candidate-communications-panel";
import { CandidateSchedulingSection } from "@/components/candidates/candidate-scheduling-section";
import {
  CandidateScorecardsPanel,
  EMPTY_SCORECARD_PANEL,
} from "@/components/candidates/candidate-scorecards-panel";
import { CandidateOffersPanel } from "@/components/offers/candidate-offers-panel";

export const dynamic = "force-dynamic";

function recommendationKey(score: number | null): keyof I18nMessages["candidate"]["recommendation"] {
  if (score == null) return "awaiting";
  if (score >= 90) return "exceptional";
  if (score >= 80) return "strong";
  if (score >= 70) return "good";
  if (score >= 60) return "possible";
  return "low";
}

export default async function CandidatePage({
  params,
  searchParams,
}: {
  params: Promise<{ id: string }>;
  searchParams: Promise<{ position?: string }>;
}) {
  const [{ id }, query, session, m] = await Promise.all([
    params,
    searchParams,
    auth(),
    getMessages(),
  ]);

  let candidate: CandidateDetail | null = null;
  try {
    const [candidateDetail, applications] = await Promise.all([
      apiGet<ApiResponseSchema<"CandidateDetail">>(`/candidates/${id}`),
      apiGet<CandidateApplicationPage>(
        `/candidates/${id}/applications?limit=100`,
      ),
    ]);
    candidate = normalizeCandidateDetail(
      candidateDetail,
      applications.items,
    );
  } catch (error) {
    // apiGet redirects when the API session expired. Preserve that framework
    // control-flow error instead of accidentally turning it into a 404.
    unstable_rethrow(error);
    notFound();
  }
  if (!candidate) notFound();

  const requestedPositionId = query.position?.trim() || null;
  const evaluatedScores = candidate.scores.filter(
    (score) => score.overall_score != null,
  );
  const bestScore = [...evaluatedScores].sort(
    (left, right) =>
      (right.overall_score ?? Number.NEGATIVE_INFINITY) -
      (left.overall_score ?? Number.NEGATIVE_INFINITY),
  )[0];
  const selectedScore =
    (requestedPositionId
      ? candidate.scores.find(
          (score) => score.position_id === requestedPositionId,
        )
      : undefined) ??
    bestScore ??
    candidate.scores[0];
  const scorecardPositionId = selectedScore?.position_id ?? null;
  const scorecardPositionQuery = scorecardPositionId
    ? `?position_id=${encodeURIComponent(scorecardPositionId)}`
    : "";
  const scorecardTemplateQuery = scorecardPositionId
    ? `?position_id=${encodeURIComponent(scorecardPositionId)}&include_inactive=true`
    : "?include_inactive=true";

  // Side fetches: interview list + open positions + reverse-match scores +
  // notes + tags. All independent — Promise.allSettled so one slow endpoint
  // doesn't block the page.
  const [
    interviewsRes,
    positionsRes,
    matchesRes,
    notesRes,
    tagsRes,
    recordingsRes,
    tasksRes,
    activitiesRes,
    assignableUsersRes,
    mergedSourcesRes,
    mergeHistoryRes,
    contactPointsRes,
    communicationAccountsRes,
    communicationTemplatesRes,
    communicationThreadsRes,
    schedulingLinksRes,
    appointmentsRes,
    scorecardsRes,
    scorecardTemplatesRes,
    offersRes,
    handoffsRes,
    offerTemplatesRes,
    offerPermissionsRes,
    enterprisePolicyRes,
    chatCapabilityRes,
  ] = await Promise.allSettled([
    apiGetAll<Interview>(`/candidates/${id}/interviews`),
    apiGetAll<Position>(`/positions`),
    apiGetAll<PositionMatch>(`/candidates/${id}/best-positions`),
    apiGetAll<CandidateNote>(`/candidates/${id}/notes`),
    apiGetAll<CandidateTag>(`/candidates/${id}/tags`),
    apiGetAll<ApiResponseSchema<"RecordingOut">>(`/candidates/${id}/recordings`),
    apiGetAll<ApiResponseSchema<"TaskOut">>(`/candidates/${id}/tasks`),
    apiGet<CandidateActivityPage>(`/candidates/${id}/activities?limit=30`),
    apiGetAll<AssignableUser>(`/users/assignable`),
    apiGetAll<CandidateMergedSource>(`/candidates/${id}/merged-sources`),
    apiGet<CandidateMergeHistoryPage>(`/candidates/${id}/merge-history?limit=20`),
    apiGetAll<CandidateContactPoint>(`/candidates/${id}/contact-points`),
    apiGetAll<CommunicationAccount>(`/communications/accounts`),
    apiGetAll<CommunicationTemplate>(`/communications/templates`),
    apiGetAll<CommunicationThread>(
      `/candidates/${id}/communication-threads`,
    ),
    apiGetAll<SchedulingLink>(`/candidates/${id}/scheduling-links`),
    apiGetAll<InterviewAppointment>(`/candidates/${id}/appointments`),
    apiGet<ScorecardPanel>(
      `/candidates/${id}/scorecards${scorecardPositionQuery}`,
    ),
    apiGetAll<ScorecardTemplate>(
      `/scorecard-templates${scorecardTemplateQuery}`,
    ),
    apiGetAll<CandidateOffer>(`/candidates/${id}/offers`),
    apiGetAll<CandidateHandoff>(`/candidates/${id}/handoffs`),
    apiGetAll<OfferTemplate>(`/offer-templates`),
    apiGet<EffectivePermissions>(`/enterprise/permissions/me`),
    apiGet<EnterpriseFieldVisibility>(`/enterprise/policy`),
    apiGet<ApiResponseSchema<"ChatCapabilityOut">>(
      `/chat/capability?candidate_id=${encodeURIComponent(id)}`,
    ),
  ]);
  for (const result of [
    interviewsRes,
    positionsRes,
    matchesRes,
    notesRes,
    tagsRes,
    recordingsRes,
    tasksRes,
    activitiesRes,
    assignableUsersRes,
    mergedSourcesRes,
    schedulingLinksRes,
    appointmentsRes,
    scorecardsRes,
    scorecardTemplatesRes,
  ]) {
    if (result.status === "rejected") unstable_rethrow(result.reason);
  }
  const interviews =
    interviewsRes.status === "fulfilled" ? interviewsRes.value : [];
  const positions =
    positionsRes.status === "fulfilled"
      ? positionsRes.value
          .filter((p) => p.status !== "closed")
          .map((p) => ({ id: p.id, title: p.title }))
      : [];
  const applicationByPosition = new Map(
    candidate.scores
      .filter((score) => score.is_active)
      .map((score) => [score.position_id, score.application_id]),
  );
  const applicationPositions = positions.flatMap((position) => {
    const applicationId = applicationByPosition.get(position.id);
    return applicationId
      ? [{ ...position, applicationId }]
      : [];
  });
  const bestMatches: PositionMatch[] =
    matchesRes.status === "fulfilled" ? matchesRes.value : [];
  const notes: CandidateNote[] =
    notesRes.status === "fulfilled" ? notesRes.value : [];
  const tags: CandidateTag[] =
    tagsRes.status === "fulfilled" ? tagsRes.value : [];
  const recordings: InterviewRecording[] =
    recordingsRes.status === "fulfilled"
      ? recordingsRes.value.map(normalizeInterviewRecording)
      : [];
  const tasks: TaskSubmission[] =
    tasksRes.status === "fulfilled"
      ? tasksRes.value.map(normalizeTaskSubmission)
      : [];
  const activities: CandidateActivityPage =
    activitiesRes.status === "fulfilled"
      ? activitiesRes.value
      : { items: [], next_cursor: null };
  const assignableUsers: AssignableUser[] =
    assignableUsersRes.status === "fulfilled" ? assignableUsersRes.value : [];
  const mergedSources: CandidateMergedSource[] =
    mergedSourcesRes.status === "fulfilled" ? mergedSourcesRes.value : [];
  const mergedSourcesFailed = mergedSourcesRes.status === "rejected";
  const mergeHistory =
    mergeHistoryRes.status === "fulfilled" ? mergeHistoryRes.value.items : [];
  const mergeHistoryFailed = mergeHistoryRes.status === "rejected";
  const contactPoints =
    contactPointsRes.status === "fulfilled" ? contactPointsRes.value : [];
  const communicationAccounts =
    communicationAccountsRes.status === "fulfilled"
      ? communicationAccountsRes.value
      : [];
  const communicationTemplates =
    communicationTemplatesRes.status === "fulfilled"
      ? communicationTemplatesRes.value
      : [];
  const communicationThreads =
    communicationThreadsRes.status === "fulfilled"
      ? communicationThreadsRes.value
      : [];
  const communicationsFailed = [
    contactPointsRes,
    communicationAccountsRes,
    communicationTemplatesRes,
    communicationThreadsRes,
  ].some((result) => result.status === "rejected");
  const schedulingLinks =
    schedulingLinksRes.status === "fulfilled" ? schedulingLinksRes.value : [];
  const appointments =
    appointmentsRes.status === "fulfilled" ? appointmentsRes.value : [];
  const scorecards =
    scorecardsRes.status === "fulfilled"
      ? scorecardsRes.value
      : EMPTY_SCORECARD_PANEL;
  const scorecardTemplates =
    scorecardTemplatesRes.status === "fulfilled"
      ? scorecardTemplatesRes.value
      : [];
  const offers = offersRes.status === "fulfilled" ? offersRes.value : [];
  const handoffs =
    handoffsRes.status === "fulfilled" ? handoffsRes.value : [];
  const offerTemplates =
    offerTemplatesRes.status === "fulfilled" ? offerTemplatesRes.value : [];

  const meEmail = session?.user?.email ?? "";
  const meId = (session?.user as { id?: string } | undefined)?.id ?? "";
  const myRole = session?.user?.role ?? "recruiter";
  const permissions =
    offerPermissionsRes.status === "fulfilled"
      ? offerPermissionsRes.value.permissions
      : {};
  const compensationRoles =
    enterprisePolicyRes.status === "fulfilled"
      ? enterprisePolicyRes.value.field_visibility.compensation ?? []
      : [];
  const canReadOffers = permissions["offers.read"] === true;
  const canManageOffers = permissions["offers.manage"] === true;
  const canWriteCandidate = permissions["candidates.write"] === true;
  const canWriteApplications = permissions["applications.write"] === true;
  const canManageInterviews =
    permissions["interviews.manage"] === true;
  const canReadInterviews = permissions["interviews.read"] === true;
  const canSubmitScorecards =
    permissions["scorecards.submit"] === true;
  const canViewOfferCompensation =
    permissions["offers.compensation"] === true &&
    compensationRoles.includes(myRole);
  const canReadCandidateContacts =
    permissions["candidates.contact"] === true &&
    contactPointsRes.status === "fulfilled";
  const canReadCommunications =
    permissions["communications.read"] === true &&
    canReadCandidateContacts &&
    communicationThreadsRes.status === "fulfilled";
  const canManageCandidateContacts =
    canWriteCandidate && canReadCandidateContacts;
  const canSendCandidateCommunications =
    permissions["communications.send"] === true &&
    canReadCandidateContacts &&
    communicationAccountsRes.status === "fulfilled";
  const canUseCandidateAi =
    permissions["ai.use"] === true &&
    permissions["ai.sensitive_context"] === true &&
    chatCapabilityRes.status === "fulfilled" &&
    chatCapabilityRes.value.available;

  const identityReviewRequired =
    candidate.source === "identity_review" ||
    candidate.parsed_cv?.identity_review_required === true;
  const ref = candRef(
    candidate.id,
    identityReviewRequired ? null : candidate.source_ref,
  );
  const isScored = selectedScore?.overall_score != null;
  const tier = scoreTier(selectedScore?.overall_score ?? null);

  // Resolve the position TITLE (not the obfuscated ref) for the selected
  // candidate-position association.
  // We piggyback the /positions list response above so this is zero extra cost.
  const positionTitle =
    selectedScore && positionsRes.status === "fulfilled"
      ? (positionsRes.value.find((p) => p.id === selectedScore.position_id)
          ?.title ?? null)
      : null;
  const positionRef = selectedScore ? prjRef(selectedScore.position_id) : null;
  const bestPositionTitle =
    bestScore && positionsRes.status === "fulfilled"
      ? (positionsRes.value.find((p) => p.id === bestScore.position_id)?.title ??
        prjRef(bestScore.position_id))
      : null;
  const isShowingBestScore =
    bestScore?.position_id === selectedScore?.position_id;

  const CRITERIA: { score: keyof Score; comment: keyof Score; label: string }[] = [
    {
      score: "score_technical_skills",
      comment: "comment_technical_skills",
      label: m.candidate.criteria.technical,
    },
    {
      score: "score_quality_of_projects",
      comment: "comment_quality_of_projects",
      label: m.candidate.criteria.projects,
    },
    {
      score: "score_previous_experience",
      comment: "comment_previous_experience",
      label: m.candidate.criteria.experience,
    },
    {
      score: "score_impact",
      comment: "comment_impact",
      label: m.candidate.criteria.impact,
    },
  ];

  const qualifiedLabel =
    (m.status as Record<string, string>)["qualified"] ?? "qualified";
  const openLabel = (m.status as Record<string, string>)["open"] ?? "open";

  return (
    <>
      <MarkViewedOnMount candidateId={candidate.id} />
      <TabFocusRefresh />
      <Topbar
        crumbs={[
          // Candidates associated with a position keep that navigation context;
          // manually-added candidates root at the candidate list.
          selectedScore
            ? { label: m.candidate.crumb, href: "/dashboard" }
            : { label: m.candidates.crumb, href: "/candidates" },
          ...(selectedScore
            ? [
                {
                  // Title-first; fall back to ref if the position got deleted
                  // out from under the score (rare but safe).
                  label: positionTitle ?? positionRef ?? "Position",
                  href: `/positions/${selectedScore.position_id}`,
                },
              ]
            : []),
          { label: ref, mono: true },
        ]}
        actions={
          <>
            {candidate.source === "hh" && candidate.source_ref ? (
              <a
                href={`https://hh.ru/resume/${candidate.source_ref.replace(/\D/g, "")}`}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-1.5 rounded-[6px] px-2.5 py-1.5 text-[12px] font-medium text-text-muted transition-colors hover:bg-bg-hover hover:text-text"
              >
                <ExternalLink className="h-3 w-3" />
                {m.candidate.openHh}
              </a>
            ) : (
              <span className="inline-flex items-center gap-1.5 rounded-[6px] px-2.5 py-1.5 text-[12px] font-medium text-text-faint">
                <ExternalLink className="h-3 w-3" />
                {m.candidate.sourceMaterial(candidate.source)}
              </span>
            )}
            {candidate.github_login ? (
              <a
                href={`https://github.com/${candidate.github_login}`}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-1.5 rounded-[6px] px-2.5 py-1.5 text-[12px] font-medium text-text-muted transition-colors hover:bg-bg-hover hover:text-text"
              >
                <GitBranch className="h-3 w-3" />
                {candidate.github_login}
              </a>
            ) : null}
            {candidate.pdf_path ? (
              <a
                href={`/api/candidates/${candidate.id}/cv`}
                download
                className="inline-flex items-center gap-1.5 rounded-[6px] border border-border bg-bg-elevated px-2.5 py-1.5 text-[12px] font-medium text-text transition-colors hover:border-border-strong hover:bg-bg-hover"
              >
                <Download className="h-3 w-3" />
                {m.candidate.export}
              </a>
            ) : null}
            {canWriteCandidate ? (
              <DeleteCandidateButton
                candidateId={candidate.id}
                candidateLabel={ref}
              />
            ) : null}
          </>
        }
      />

      <div className="flex h-[calc(100vh-53px)]">
        <div className="min-h-0 min-w-0 flex-1 overflow-y-auto">
          <div className="px-7 pb-16 pt-7">
            {identityReviewRequired ? (
              <div
                role="status"
                className="mb-6 flex max-w-4xl items-start gap-3 rounded-lg border border-score-mid/35 bg-score-mid/10 px-4 py-3 text-score-mid"
              >
                <AlertTriangle aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0" />
                <div>
                  <p className="text-[13px] font-semibold">
                    {m.candidate.identityReviewTitle}
                  </p>
                  <p className="mt-1 text-[12px] leading-5">
                    {m.candidate.identityReviewDescription}
                  </p>
                </div>
              </div>
            ) : null}
            {/* Candidate header */}
            <div className="mb-7">
              <div className="mb-3.5 flex items-center gap-2">
                <SourceBadge source={candidate.source} />
                {isScored ? (
                  <StatusBadge status="qualified" label={qualifiedLabel} />
                ) : (
                  <StatusBadge status="open" label={openLabel} />
                )}
                {isScored ? (
                  <span className="font-mono text-[11px] text-text-faint">
                    {m.candidate.scoredMeta(
                      m.format.relativeTime(candidate.created_at),
                    )}
                  </span>
                ) : (
                  <span className="font-mono text-[11px] text-text-faint">
                    {m.candidate.addedMeta(
                      m.format.relativeTime(candidate.created_at),
                    )}
                  </span>
                )}
              </div>
              <div className="flex flex-col items-start justify-between gap-6 md:flex-row md:items-start">
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <h1 className="font-mono text-[18px] font-semibold tracking-[-0.01em]">
                      {ref}
                    </h1>
                    {canWriteCandidate ? (
                      <FavoriteButton
                        candidateId={candidate.id}
                        initial={candidate.is_favorite}
                        size="md"
                        parentPath={
                          selectedScore
                            ? `/positions/${selectedScore.position_id}`
                            : undefined
                        }
                      />
                    ) : null}
                  </div>
                  {selectedScore ? (
                    <div className="mt-1.5 flex flex-col items-start gap-2 text-[13px] text-text-muted">
                      <p>
                        {isScored && isShowingBestScore && evaluatedScores.length > 1
                          ? m.candidate.bestMatchOf(evaluatedScores.length)
                          : m.candidate.scoredAgainst}{" "}
                        <Link
                          href={`/positions/${selectedScore.position_id}`}
                          className="border-b border-dashed border-text-faint pb-px text-text transition-colors hover:border-text"
                        >
                          {positionTitle ?? positionRef}
                        </Link>
                      </p>
                      {isScored && bestScore && !isShowingBestScore ? (
                        <Link
                          href={`/candidates/${candidate.id}?position=${bestScore.position_id}`}
                          className="inline-flex flex-wrap items-center gap-x-1.5 gap-y-1 rounded-full border border-score-high/25 bg-score-high/10 px-2.5 py-1 text-[11.5px] text-text transition-colors hover:border-score-high/45 hover:bg-score-high/15"
                        >
                          <span className="text-text-muted">
                            {m.candidate.bestMatchOf(evaluatedScores.length)}
                          </span>
                          <span className="font-medium">{bestPositionTitle}</span>
                          <span className="font-mono font-semibold tabular-nums text-score-high">
                            {bestScore.overall_score}/100
                          </span>
                        </Link>
                      ) : null}
                    </div>
                  ) : (
                    <p className="mt-1.5 text-[13px] italic text-text-faint">
                      {m.candidate.addedManually}
                    </p>
                  )}
                </div>

                {isScored && selectedScore ? (
                  <div className="flex shrink-0 items-center gap-4 rounded-lg border border-border bg-bg-elevated px-4 py-3">
                    <span
                      className="font-mono text-[32px] font-semibold leading-none tracking-[-0.03em] tabular-nums"
                      style={{
                        color: tier ? SCORE_VAR[tier] : "var(--text-faint)",
                      }}
                    >
                      {selectedScore.overall_score}
                    </span>
                    <div className="border-l border-border pl-4">
                      <div className="mb-0.5 text-[10px] font-medium uppercase tracking-[0.08em] text-text-faint">
                        {m.candidate.overall}
                      </div>
                      <div className="text-[12px] font-medium text-text">
                        {
                          m.candidate.recommendation[
                            recommendationKey(selectedScore.overall_score)
                          ]
                        }
                      </div>
                    </div>
                  </div>
                ) : null}
              </div>

              {/* Tags row — sits under the score header so they're at the
                  glance line, not buried in side data. */}
              <div className="mt-4">
                <TagsRow
                  candidateId={candidate.id}
                  initial={tags}
                  canManage={canWriteCandidate}
                />
              </div>
            </div>

            {/* Criteria breakdown */}
            {isScored && selectedScore ? (
              <>
                <div className="mb-2.5 px-0.5 text-[11px] font-medium uppercase tracking-[0.08em] text-text-muted">
                  {m.candidate.criteriaBreakdown}
                </div>
                <div className="mb-7 grid grid-cols-1 gap-2.5 md:auto-rows-fr md:grid-cols-2">
                  {CRITERIA.map((c, i) => (
                    <div
                      key={String(c.score)}
                      className="reveal h-full"
                      style={{ animationDelay: `${i * 40}ms` }}
                    >
                      <CriterionCard
                        label={c.label}
                        score={selectedScore[c.score] as number | null}
                        comment={selectedScore[c.comment] as string | null}
                        emptyComment={m.candidate.noComment}
                        delayMs={200 + i * 60}
                      />
                    </div>
                  ))}
                </div>
              </>
            ) : (
              <div className="mb-7 rounded-lg border border-border bg-bg-elevated px-5 py-4 text-[13px] text-text-muted">
                {m.candidate.notScored}
              </div>
            )}

            {selectedScore ? (
              <div className="mb-7">
                <WorkControls
                  key={selectedScore.position_id}
                  candidateId={candidate.id}
                  positionId={selectedScore.position_id}
                  positionTitle={
                    positionTitle ?? positionRef ?? selectedScore.position_id
                  }
                  assignableUsers={assignableUsers}
                  initialOwnerUserId={selectedScore.owner_user_id}
                  initialDueDate={selectedScore.next_action_due_date}
                  canEdit={canWriteApplications}
                />
              </div>
            ) : null}

            <div className="mb-7">
              <CandidateScorecardsPanel
                key={`scorecards-${scorecardPositionId ?? "none"}`}
                candidateId={candidate.id}
                positionId={scorecardPositionId}
                positionTitle={positionTitle}
                currentUserId={meId}
                assignableUsers={assignableUsers}
                initialPanel={scorecards}
                initialTemplates={scorecardTemplates}
                canManageScorecards={canManageInterviews}
                canSubmitScorecards={canSubmitScorecards}
                canManageDecisions={canManageInterviews}
                initialPanelFailed={scorecardsRes.status === "rejected"}
                initialTemplatesFailed={scorecardTemplatesRes.status === "rejected"}
              />
            </div>

            <div className="mb-7">
              <CandidateOffersPanel
                candidateId={candidate.id}
                positions={positions.filter((position) =>
                  candidate.scores.some(
                    (score) => score.position_id === position.id,
                  ),
                )}
                templates={offerTemplates}
                initialOffers={offers}
                initialHandoffs={handoffs}
                canRead={canReadOffers}
                canManage={canManageOffers}
                canViewCompensation={canViewOfferCompensation}
                initialOffersFailed={offersRes.status === "rejected"}
                initialHandoffsFailed={handoffsRes.status === "rejected"}
              />
            </div>

            <div className="mb-7">
              <CandidateSchedulingSection
                key={`scheduling-${selectedScore?.position_id ?? "none"}`}
                candidateId={candidate.id}
                interviews={interviews}
                positions={positions.filter((position) =>
                  candidate.scores.some(
                    (score) => score.position_id === position.id,
                  ),
                )}
                defaultPositionId={selectedScore?.position_id ?? null}
                assignableUsers={assignableUsers}
                currentUserId={meId}
                initialLinks={schedulingLinks}
                initialAppointments={appointments}
                initialLinksFailed={schedulingLinksRes.status === "rejected"}
                initialAppointmentsFailed={appointmentsRes.status === "rejected"}
              />
            </div>

            {canReadCandidateContacts &&
            (canReadCommunications || canManageCandidateContacts) ? (
              <div className="mb-7">
                <CandidateCommunicationsPanel
                  candidateId={candidate.id}
                  positions={positions}
                  initialContacts={contactPoints}
                  initialAccounts={communicationAccounts}
                  initialTemplates={communicationTemplates}
                  initialThreads={communicationThreads}
                  initialLoadFailed={
                    canReadCommunications && communicationsFailed
                  }
                  canManageContacts={canManageCandidateContacts}
                  canSend={canSendCandidateCommunications}
                  canReadHistory={canReadCommunications}
                />
              </div>
            ) : null}

            {/* Existing GitHub evidence remains readable. New external
                enrichment stays hidden until the durable connector is enabled. */}
            {candidate.github_login && candidate.github_data ? (
              <div className="mb-7">
                <GitHubSection
                  candidateId={candidate.id}
                  login={candidate.github_login}
                  data={candidate.github_data}
                  fetchedAt={candidate.github_fetched_at}
                  canManage={canWriteCandidate}
                  enrichmentAvailable={false}
                />
              </div>
            ) : null}

            {/* Recruiter notes — feed of free-form notes by the team. */}
            <div className="mb-7">
              <NotesSection
                candidateId={candidate.id}
                initial={notes}
                currentUserEmail={meEmail}
                canWrite={canWriteCandidate}
              />
            </div>

            {/* Reverse matching: which open positions suit this candidate */}
            <div className="mb-7">
              <BestPositionsSection
                candidateId={candidate.id}
                initial={bestMatches}
                canScore={canWriteCandidate && canUseCandidateAi}
              />
            </div>

            {/* Keep the durable audit history available without allowing a
                large scoring batch to push decision-making content away. */}
            <div className="mb-7">
              <ActivityTimeline
                candidateId={candidate.id}
                initialPage={activities}
                initialLoadFailed={activitiesRes.status === "rejected"}
              />
            </div>

            {interviews.length > 0 || canManageInterviews ? (
              <div className="mb-7">
                <InterviewSection
                  key={`interviews-${selectedScore?.position_id ?? "none"}`}
                  candidateId={candidate.id}
                  initialInterviews={interviews}
                  positions={applicationPositions}
                  assignableUsers={assignableUsers}
                  currentUserId={meId}
                  defaultPositionId={selectedScore?.position_id ?? null}
                  canManage={canManageInterviews}
                  canSubmitFeedback={canSubmitScorecards}
                />
              </div>
            ) : null}

            {/* Interview recordings — pipeline-written transcript + score */}
            {canReadInterviews ? (
              <div className="mb-7">
                <RecordingsSection
                  candidateId={candidate.id}
                  initial={recordings}
                  canManage={canManageInterviews}
                />
              </div>
            ) : null}

            {/* Technical task submissions — AI-absorbed + scored */}
            <div className="mb-7">
              <TaskSubmissionSection
                candidateId={candidate.id}
                initial={tasks}
                canManage={canManageInterviews}
                evaluationAvailable={false}
              />
            </div>

            {mergedSourcesFailed ||
            (canWriteCandidate && mergeHistoryFailed) ||
            (canWriteCandidate && mergeHistory.length > 0) ||
            mergedSources.some((source) => !source.is_primary) ? (
              <div className="mb-7">
                <MergedSourcesSection
                  sources={mergedSources}
                  history={canWriteCandidate ? mergeHistory : []}
                  loadFailed={
                    mergedSourcesFailed ||
                    (canWriteCandidate && mergeHistoryFailed)
                  }
                />
              </div>
            ) : null}

            {/* Source data */}
            <div className="mb-2.5 px-0.5 text-[11px] font-medium uppercase tracking-[0.08em] text-text-muted">
              {m.candidate.sourceData}
            </div>
            <div className="flex flex-col gap-2">
              {candidate.raw_cv ? (
                <Collapsible
                  title={m.candidate.resumeText}
                  meta={m.candidate.resumeChars(candidate.raw_cv.length)}
                >
                  <pre className="whitespace-pre-wrap py-3 text-[12.5px] leading-[1.7] text-text-muted">
                    {candidate.raw_cv}
                  </pre>
                </Collapsible>
              ) : null}
              {candidate.parsed_cv ? (
                <Collapsible
                  title={m.candidate.parsedJson}
                  meta={m.candidate.parsedJsonMeta(
                    approxBytes(JSON.stringify(candidate.parsed_cv)),
                  )}
                >
                  <JsonView value={candidate.parsed_cv} />
                </Collapsible>
              ) : null}
              {!candidate.raw_cv && !candidate.parsed_cv ? (
                <div className="rounded-lg border border-border bg-bg-elevated px-5 py-4 text-[13px] text-text-muted">
                  {m.candidate.noResume}
                </div>
              ) : null}
            </div>
          </div>
        </div>

        {/* Inline collapsible chat — pushes content left when open */}
        {session?.user && canUseCandidateAi ? (
          <CollapsibleChat
            scope="candidate"
            candidateId={candidate.id}
            scopeRef={ref}
          />
        ) : null}
      </div>
    </>
  );
}

function approxBytes(s: string): string {
  const bytes = new TextEncoder().encode(s).length;
  if (bytes < 1024) return `${bytes} b`;
  return `${(bytes / 1024).toFixed(1)} kb`;
}
