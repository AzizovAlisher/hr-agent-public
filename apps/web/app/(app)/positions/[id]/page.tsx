import { notFound } from "next/navigation";

import { auth } from "@/auth";
import { apiGet } from "@/lib/api-client";
import { prjRef } from "@/lib/format";
import type { ApiResponseSchema } from "@/lib/generated/contracts";
import { getMessages } from "@/lib/i18n";
import type { PositionDetail, RankedCandidatePage } from "@/lib/types";
import { CollapsibleChat } from "@/components/chat/collapsible-chat";
import { TabFocusRefresh } from "@/components/app/tab-focus-refresh";
import { Topbar } from "@/components/app/topbar";
import { UploadForm } from "@/components/candidates/upload-form";
import { CandidateProfileCard } from "@/components/positions/candidate-profile-card";
import { PositionHeader } from "@/components/positions/position-header";
import { PositionNotes } from "@/components/positions/position-notes";
import { PositionVisibilityCard } from "@/components/positions/position-visibility-card";
import { PositionTopbarActions } from "@/components/positions/position-topbar-actions";
import { CandidatesView } from "@/components/positions/candidates-view";
import { ShareApplyLink } from "@/components/positions/share-apply-link";
import { ShareShortlistButton } from "@/components/positions/share-shortlist";
import { BulkUploadDialog } from "@/components/candidates/bulk-upload-dialog";
import { getCurrentPermissionMap } from "@/lib/current-permissions";
import { getCandidateWorkspaceAccess, hasPermission } from "@/lib/permissions";

export const dynamic = "force-dynamic";

export default async function PositionPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  const [session, m, permissionMap] = await Promise.all([
    auth(),
    getMessages(),
    getCurrentPermissionMap(),
  ]);
  const access = getCandidateWorkspaceAccess(permissionMap);

  let position: PositionDetail | null = null;
  let pipeline: RankedCandidatePage | null = null;
  try {
    [position, pipeline] = await Promise.all([
      apiGet<PositionDetail>(`/positions/${id}`),
      apiGet<RankedCandidatePage>(`/positions/${id}/applications?limit=50`),
    ]);
  } catch {
    notFound();
  }
  if (!position || !pipeline) notFound();

  const canAttemptRescore =
    access.writeCandidates &&
    access.useSensitiveAi &&
    hasPermission(permissionMap, "requisitions.compensation");
  let chatAvailable = false;
  let rescoreAvailable = false;
  if (session?.user && access.useSensitiveAi) {
    try {
      const capability = await apiGet<ApiResponseSchema<"ChatCapabilityOut">>(
        `/chat/capability?position_id=${encodeURIComponent(id)}`,
      );
      chatAvailable = capability.available;
    } catch {
      // The API is authoritative for field policy, assignment scope, and
      // tenant AI availability. A denied capability must not leave a control
      // that can only fail.
    }
  }
  if (session?.user && canAttemptRescore) {
    try {
      const capability = await apiGet<ApiResponseSchema<"ChatCapabilityOut">>(
        `/positions/${encodeURIComponent(id)}/rescore-capability`,
      );
      rescoreAvailable = capability.available;
    } catch {
      // Rescore has a deliberately narrower field contract than chat. Keep
      // this probe independent so custom field policy remains authoritative.
    }
  }

  const projectRef = prjRef(position.id);

  return (
    <>
      <TabFocusRefresh />
      <Topbar
        crumbs={[
          { label: m.position.crumb, href: "/dashboard" },
          { label: position.title },
        ]}
        actions={
          <>
            {access.writeCandidates ? (
              <BulkUploadDialog positionId={position.id} />
            ) : null}
            {access.writeCandidates && position.public_apply_enabled && position.status === "open" ? (
              <ShareApplyLink positionId={position.id} />
            ) : null}
            {access.exportCandidates ? (
              <ShareShortlistButton
                positionId={position.id}
                canIncludeContact={access.viewCandidateContacts}
              />
            ) : null}
            {access.writeCandidates ? (
              <PositionTopbarActions
                positionId={position.id}
                status={position.status}
                publicApplyEnabled={position.public_apply_enabled}
                canRescore={
                  canAttemptRescore && rescoreAvailable
                }
              />
            ) : null}
          </>
        }
      />

      <div className="flex h-[calc(100vh-53px)]">
        <div className="min-h-0 min-w-0 flex-1 overflow-y-auto">
          <div className="px-7 pb-16 pt-7">
            <PositionHeader
              positionId={position.id}
              initialTitle={position.title}
              initialDescription={position.description}
              initialFullDescription={position.full_description}
              initialStatus={position.status}
              initialRequirements={position.requirements}
              projectRef={projectRef}
              canEdit={access.writeCandidates}
            />

            {/* Who we're looking for — the brief a recruiter screens against.
                Sits directly under the header so it reads as part of the
                vacancy itself rather than as an afterthought. */}
            <div className="mb-4">
              <CandidateProfileCard
                positionId={position.id}
                initial={position.candidate_profile}
                confidential={position.sensitivity !== "public"}
                canEdit={access.writeCandidates}
                canGenerate={false}
              />
            </div>

            {/* What the outside world sees */}
            <div className="mb-4">
              <PositionVisibilityCard
                positionId={position.id}
                initialSensitivity={position.sensitivity}
                initialPublicDescription={position.public_description}
                canEdit={access.writeCandidates}
              />
            </div>

            {/* Notes */}
            <div className="mb-6">
              <PositionNotes
                positionId={position.id}
                initial={position.internal_notes}
                canEdit={access.writeCandidates}
              />
            </div>

            {/* Add candidate — collapsed by default so the ranked list (the
                point of this page) sits near the top; bulk upload also lives in
                the topbar. */}
            {access.writeCandidates ? <details className="group mb-6 overflow-hidden rounded-lg border border-border bg-bg-elevated">
              <summary className="flex cursor-pointer list-none items-center justify-between gap-2 px-4 py-3 text-[13px] font-medium text-text-muted transition-colors hover:text-text">
                <span>{m.position.addCandidate}</span>
                <span className="font-mono text-[13px] leading-none text-text-faint transition-transform group-open:rotate-45">
                  +
                </span>
              </summary>
              <div className="border-t border-border">
                <UploadForm positionId={position.id} />
              </div>
            </details> : null}

            {/* Ranked candidates */}
            <div className="mb-6">
              <CandidatesView
                candidates={pipeline.items}
                nextCursor={pipeline.next_cursor}
                positionId={position.id}
                canWriteCandidates={access.writeCandidates}
                canChangeStage={access.changeApplicationStage}
              />
            </div>
          </div>
        </div>

        {/* Inline collapsible chat — pushes content left when open */}
        {session?.user && chatAvailable ? (
          <CollapsibleChat
            scope="position"
            positionId={position.id}
            scopeRef={projectRef}
            badge={position.chat_session_count ?? null}
          />
        ) : null}
      </div>
    </>
  );
}
