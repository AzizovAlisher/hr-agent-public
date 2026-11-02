"use client";

import {
  Check,
  ClipboardCheck,
  Eye,
  EyeOff,
  Loader2,
  Plus,
  RotateCcw,
  ShieldCheck,
  UserRoundCheck,
  X,
} from "lucide-react";
import { useEffect, useMemo, useState, useTransition } from "react";
import { toast } from "sonner";

import { useI18n } from "@/components/i18n/locale-provider";
import { Button } from "@/components/ui/button";
import {
  assignCandidateScorecardsAction,
  getCandidateScorecardsAction,
  listScorecardTemplatesAction,
  saveHiringDecisionAction,
  submitScorecardAction,
  updateScorecardDraftAction,
  voteHiringDecisionAction,
} from "@/lib/actions";
import { schedulingDateTime } from "@/lib/scheduling-time";
import type {
  AssignableUser,
  HiringDecision,
  HiringRecommendation,
  ScorecardAssignment,
  ScorecardCriterion,
  ScorecardPanel,
  ScorecardTemplate,
} from "@/lib/types";
import { cn } from "@/lib/utils";


const EMPTY_PANEL: ScorecardPanel = {
  assignments: [],
  revealed: false,
  aggregate_average: null,
  completion_count: 0,
  assignment_count: 0,
  decision: null,
};


export function CandidateScorecardsPanel({
  candidateId,
  positionId,
  positionTitle,
  currentUserId,
  assignableUsers,
  initialPanel,
  initialTemplates,
  canManageScorecards,
  canSubmitScorecards,
  canManageDecisions,
  initialPanelFailed,
  initialTemplatesFailed,
}: {
  candidateId: string;
  positionId: string | null;
  positionTitle: string | null;
  currentUserId: string;
  assignableUsers: AssignableUser[];
  initialPanel: ScorecardPanel;
  initialTemplates: ScorecardTemplate[];
  canManageScorecards: boolean;
  canSubmitScorecards: boolean;
  canManageDecisions: boolean;
  initialPanelFailed: boolean;
  initialTemplatesFailed: boolean;
}) {
  const { locale, m } = useI18n();
  const copy = m.scorecards;
  const [panel, setPanel] = useState(initialPanel);
  const [templates, setTemplates] = useState(initialTemplates);
  const [panelFailed, setPanelFailed] = useState(initialPanelFailed);
  const [templatesFailed, setTemplatesFailed] = useState(initialTemplatesFailed);
  const [assignOpen, setAssignOpen] = useState(false);
  const [templateId, setTemplateId] = useState(initialTemplates[0]?.id ?? "");
  const [reviewerIds, setReviewerIds] = useState<string[]>([]);
  const [dueAt, setDueAt] = useState("");
  const [pending, startTransition] = useTransition();
  const assignableTemplates = useMemo(
    () => templates.filter((template) => template.is_active),
    [templates],
  );
  const templateById = useMemo(
    () => new Map(templates.map((template) => [template.id, template])),
    [templates],
  );
  const assignDisabledReason = !positionId
    ? copy.noPosition
    : panel.decision != null
      ? copy.assignmentLocked
      : templatesFailed
        ? copy.templatesLoadFailed
        : assignableTemplates.length === 0
          ? copy.noTemplates
          : null;
  const assignDisabledReasonId = !positionId
    ? "scorecard-no-position"
    : panel.decision != null
      ? "scorecard-assignment-locked"
      : templatesFailed
        ? "scorecard-templates-load-failed"
        : assignableTemplates.length === 0
          ? "scorecard-no-active-templates"
          : undefined;

  useEffect(() => {
    if (assignableTemplates.some((template) => template.id === templateId)) return;
    setTemplateId(assignableTemplates[0]?.id ?? "");
  }, [assignableTemplates, templateId]);

  function toggleReviewer(userId: string) {
    setReviewerIds((current) =>
      current.includes(userId)
        ? current.filter((id) => id !== userId)
        : [...current, userId],
    );
  }

  function assign(event: React.FormEvent) {
    event.preventDefault();
    if (!positionId || !templateId || reviewerIds.length === 0 || pending) return;
    startTransition(async () => {
      const result = await assignCandidateScorecardsAction(candidateId, {
        position_id: positionId,
        template_id: templateId,
        user_ids: reviewerIds,
        due_at: dueAt ? new Date(dueAt).toISOString() : null,
      });
      if (!result.ok) {
        toast.error(copy.saveFailed);
        return;
      }
      setPanel(result.data);
      setPanelFailed(false);
      setAssignOpen(false);
      setReviewerIds([]);
      setDueAt("");
      toast.success(copy.assigned);
    });
  }

  function retryPanel() {
    startTransition(async () => {
      const result = await getCandidateScorecardsAction(candidateId, positionId);
      if (!result.ok) {
        setPanelFailed(true);
        return;
      }
      setPanel(result.data);
      setPanelFailed(false);
    });
  }

  function retryTemplates() {
    startTransition(async () => {
      const result = await listScorecardTemplatesAction({
        positionId,
        includeInactive: true,
      });
      if (!result.ok) {
        setTemplatesFailed(true);
        return;
      }
      setTemplates(result.data);
      setTemplatesFailed(false);
    });
  }

  function updateAssignment(next: ScorecardAssignment) {
    setPanel((current) => ({
      ...current,
      assignments: current.assignments.map((assignment) =>
        assignment.id === next.id ? next : assignment,
      ),
    }));
  }

  return (
    <section className="overflow-hidden rounded-lg border border-border bg-bg-elevated">
      <div className="flex flex-col gap-3 border-b border-border px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h2 className="text-[13px] font-semibold text-text">{copy.title}</h2>
          <p className="mt-0.5 text-[11px] text-text-faint">{copy.subtitle}</p>
        </div>
        {canManageScorecards ? <Button
          size="sm"
          variant={assignOpen ? "ghost" : "outline"}
          onClick={() => setAssignOpen((current) => !current)}
          disabled={!assignOpen && assignDisabledReason != null}
          aria-describedby={!assignOpen ? assignDisabledReasonId : undefined}
          title={!assignOpen ? (assignDisabledReason ?? undefined) : undefined}
        >
          {assignOpen ? <X className="h-3.5 w-3.5" /> : <Plus className="h-3.5 w-3.5" />}
          {assignOpen ? m.common.cancel : copy.assign}
        </Button> : null}
      </div>

      {!positionId ? (
        <p id="scorecard-no-position" className="px-4 py-5 text-[12px] text-text-muted">{copy.noPosition}</p>
      ) : canManageScorecards && panel.decision != null ? (
        <p id="scorecard-assignment-locked" className="border-b border-border px-4 py-3 text-[11.5px] text-text-muted">
          {copy.assignmentLocked}
        </p>
      ) : null}

      {assignOpen && positionId ? (
        <form onSubmit={assign} className="border-b border-border bg-bg/35 px-4 py-4">
          <h3 className="text-[12px] font-semibold text-text">{copy.assignTitle}</h3>
          <div className="mt-3 grid gap-3 md:grid-cols-2">
            <Field label={copy.template}>
              <select value={templateId} onChange={(event) => setTemplateId(event.target.value)} className={controlClass}>
                {assignableTemplates.map((template) => (
                  <option key={template.id} value={template.id}>{template.name}</option>
                ))}
              </select>
            </Field>
            <Field label={copy.dueAt}>
              <input type="datetime-local" value={dueAt} onChange={(event) => setDueAt(event.target.value)} className={controlClass} />
            </Field>
          </div>
          <fieldset className="mt-3">
            <legend className="text-[10.5px] font-medium text-text-muted">{copy.teamMembers}</legend>
            <div className="mt-2 flex flex-wrap gap-2">
              {assignableUsers.map((user) => {
                const selected = reviewerIds.includes(user.id);
                return (
                  <label key={user.id} className={cn(
                    "flex cursor-pointer items-center gap-2 rounded-[6px] border px-2.5 py-2 text-[11.5px] transition-colors",
                    selected ? "border-accent-border bg-accent-muted text-text" : "border-border bg-bg-elevated text-text-muted",
                  )}>
                    <input type="checkbox" checked={selected} onChange={() => toggleReviewer(user.id)} className="sr-only" />
                    <span className="flex h-4 w-4 items-center justify-center rounded-[4px] border border-border-strong">
                      {selected ? <Check className="h-3 w-3" /> : null}
                    </span>
                    {user.email}
                  </label>
                );
              })}
            </div>
            {reviewerIds.length === 0 ? <p className="mt-2 text-[10.5px] text-text-faint">{copy.selectMembers}</p> : null}
          </fieldset>
          <div className="mt-4 flex justify-end">
            <Button type="submit" size="sm" disabled={pending || reviewerIds.length === 0 || !templateId}>
              {pending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <UserRoundCheck className="h-3.5 w-3.5" />}
              {pending ? copy.assigning : copy.assign}
            </Button>
          </div>
        </form>
      ) : null}

      {templatesFailed ? (
        <div className="flex items-center justify-between gap-3 border-b border-border px-4 py-3 text-[11.5px] text-text-muted">
          <span id="scorecard-templates-load-failed">{copy.templatesLoadFailed}</span>
          <Button size="sm" variant="ghost" onClick={retryTemplates} disabled={pending}>{copy.retry}</Button>
        </div>
      ) : canManageScorecards && positionId && assignableTemplates.length === 0 ? (
        <p id="scorecard-no-active-templates" className="border-b border-border px-4 py-3 text-[11.5px] text-text-muted">
          {copy.noTemplates}
        </p>
      ) : null}

      {panelFailed && panel.assignments.length === 0 ? (
        <div className="flex flex-col items-center gap-3 px-4 py-10 text-center">
          <p className="text-[12px] text-text-muted">{copy.loadFailed}</p>
          <Button size="sm" variant="outline" onClick={retryPanel} disabled={pending}>
            {pending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RotateCcw className="h-3.5 w-3.5" />}
            {copy.retry}
          </Button>
        </div>
      ) : positionId ? (
        <>
          <div className="grid gap-3 border-b border-border px-4 py-4 sm:grid-cols-3">
            <Summary label={copy.submitted} value={copy.progress(panel.completion_count, panel.assignment_count)} icon={<ClipboardCheck className="h-4 w-4" />} />
            <Summary label={copy.aggregate} value={panel.aggregate_average == null ? "—" : `${panel.aggregate_average.toFixed(2)}/5`} icon={<ShieldCheck className="h-4 w-4" />} />
            <Summary label={panel.revealed ? copy.revealed : copy.blindTitle} value={panel.revealed ? copy.revealed : copy.feedbackHidden} icon={panel.revealed ? <Eye className="h-4 w-4" /> : <EyeOff className="h-4 w-4" />} />
          </div>

          {!panel.revealed && panel.assignment_count > 0 ? (
            <div className="border-b border-border bg-accent-muted/40 px-4 py-3">
              <p className="text-[11.5px] font-medium text-text">{copy.blindTitle}</p>
              <p className="mt-0.5 text-[10.5px] leading-4 text-text-muted">{copy.blindDesc}</p>
            </div>
          ) : null}

          <div className="px-4 py-4">
            {panel.assignments.length === 0 ? (
              <p className="py-6 text-center text-[12px] text-text-muted">{copy.empty}</p>
            ) : (
              <div className="flex flex-col gap-3">
                {panel.assignments.map((assignment) => (
                  <AssignmentCard
                    key={`${assignment.id}:${assignment.updated_at}`}
                    candidateId={candidateId}
                    assignment={assignment}
                    template={assignment.template_id ? (templateById.get(assignment.template_id) ?? null) : null}
                    canSubmit={canSubmitScorecards}
                    onAssignment={updateAssignment}
                    onPanel={setPanel}
                  />
                ))}
              </div>
            )}
          </div>

          <DecisionSection
            candidateId={candidateId}
            positionId={positionId}
            positionTitle={positionTitle}
            currentUserId={currentUserId}
            panel={panel}
            canManage={canManageDecisions}
            onDecision={(decision) => setPanel((current) => ({ ...current, decision }))}
          />
        </>
      ) : null}
    </section>
  );
}


function AssignmentCard({
  candidateId,
  assignment,
  template,
  canSubmit,
  onAssignment,
  onPanel,
}: {
  candidateId: string;
  assignment: ScorecardAssignment;
  template: ScorecardTemplate | null;
  canSubmit: boolean;
  onAssignment: (assignment: ScorecardAssignment) => void;
  onPanel: (panel: ScorecardPanel) => void;
}) {
  const { locale, m } = useI18n();
  const copy = m.scorecards;
  const criteria: ScorecardCriterion[] =
    template?.criteria ??
    (assignment.ratings ?? []).map((rating, index) => ({
      id: rating.criterion_id ?? rating.id,
      key: rating.criterion_key,
      label: rating.criterion_label,
      description: null,
      weight: rating.weight,
      position: index,
      is_required: true,
    }));
  const existing = new Map(
    (assignment.ratings ?? [])
      .filter((rating) => rating.criterion_id)
      .map((rating) => [rating.criterion_id!, rating]),
  );
  const [ratings, setRatings] = useState<Record<string, { rating: number | null; comment: string }>>(
    Object.fromEntries(
      criteria.map((criterion) => {
        const value = existing.get(criterion.id);
        return [criterion.id, { rating: value?.rating ?? null, comment: value?.comment ?? "" }];
      }),
    ),
  );
  const [pending, startTransition] = useTransition();
  const editable =
    canSubmit && assignment.is_mine && assignment.status === "draft" && template != null;

  function payload() {
    return criteria.flatMap((criterion) => {
      const value = ratings[criterion.id];
      return value?.rating
        ? [{ criterion_id: criterion.id, rating: value.rating, comment: value.comment.trim() || null }]
        : [];
    });
  }

  function requiredComplete() {
    return criteria.every(
      (criterion) => !criterion.is_required || ratings[criterion.id]?.rating != null,
    );
  }

  function saveDraft() {
    const entries = payload();
    if (entries.length === 0 || pending) {
      toast.error(copy.completeRequired);
      return;
    }
    startTransition(async () => {
      const result = await updateScorecardDraftAction(assignment.id, entries);
      if (!result.ok) {
        toast.error(copy.saveFailed);
        return;
      }
      onAssignment(result.data);
      toast.success(copy.draftSaved);
    });
  }

  function submit() {
    if (!requiredComplete()) {
      toast.error(copy.completeRequired);
      return;
    }
    if (pending || !window.confirm(copy.submitConfirm)) return;
    const entries = payload();
    startTransition(async () => {
      const saved = await updateScorecardDraftAction(assignment.id, entries);
      if (!saved.ok) {
        toast.error(copy.saveFailed);
        return;
      }
      const result = await submitScorecardAction(assignment.id, candidateId);
      if (!result.ok) {
        toast.error(copy.saveFailed);
        return;
      }
      onPanel(result.data);
      toast.success(copy.submitted);
    });
  }

  return (
    <article className="rounded-[9px] border border-border bg-bg px-3.5 py-3.5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-[12.5px] font-semibold text-text">{assignment.template_name}</h3>
            {assignment.is_mine ? <span className="rounded-[4px] bg-accent-muted px-1.5 py-0.5 text-[9.5px] text-primary">{copy.mine}</span> : null}
          </div>
          <p className="mt-1 text-[10.5px] text-text-faint">{assignment.assignee_email}</p>
        </div>
        <div className="text-right">
          <span className="rounded-[4px] bg-bg-active px-2 py-1 text-[10px] text-text-muted">
            {assignment.status === "submitted" ? copy.submitted : copy.draft}
          </span>
          {assignment.due_at ? <p className="mt-1.5 text-[9.5px] text-text-faint">{copy.dueAt}: {schedulingDateTime(assignment.due_at, locale)}</p> : null}
        </div>
      </div>

      {assignment.ratings === null ? (
        <div className="mt-3 flex items-center gap-2 rounded-[6px] border border-dashed border-border px-3 py-3 text-[11px] text-text-faint">
          <EyeOff className="h-3.5 w-3.5" />
          {copy.feedbackHidden}
        </div>
      ) : editable ? (
        <div className="mt-4 flex flex-col gap-4">
          {criteria.map((criterion) => {
            const value = ratings[criterion.id] ?? { rating: null, comment: "" };
            return (
              <div key={criterion.id} className="rounded-[7px] border border-border bg-bg-elevated px-3 py-3">
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <p className="text-[11.5px] font-medium text-text">
                      {criterion.label} {criterion.is_required ? <span className="text-destructive">*</span> : null}
                    </p>
                    {criterion.description ? <p className="mt-1 text-[10.5px] leading-4 text-text-faint">{criterion.description}</p> : null}
                  </div>
                  <span className="font-mono text-[9.5px] text-text-faint">×{criterion.weight}</span>
                </div>
                <div className="mt-3 flex flex-wrap gap-1.5" role="radiogroup" aria-label={`${copy.rating}: ${criterion.label}`}>
                  {[1, 2, 3, 4, 5].map((rating) => (
                    <button
                      key={rating}
                      type="button"
                      role="radio"
                      aria-checked={value.rating === rating}
                      title={copy.ratingScale[String(rating)]}
                      onClick={() => setRatings((current) => ({ ...current, [criterion.id]: { ...value, rating } }))}
                      className={cn(
                        "h-8 w-8 rounded-[6px] border font-mono text-[12px] font-semibold transition-colors",
                        value.rating === rating ? "border-primary bg-primary text-primary-foreground" : "border-border bg-bg text-text-muted hover:border-accent-border",
                      )}
                    >
                      {rating}
                    </button>
                  ))}
                </div>
                <textarea
                  value={value.comment}
                  onChange={(event) => setRatings((current) => ({ ...current, [criterion.id]: { ...value, comment: event.target.value } }))}
                  placeholder={copy.commentPlaceholder}
                  rows={2}
                  maxLength={4000}
                  className="mt-3 block w-full resize-y rounded-[6px] border border-border bg-bg px-2.5 py-2 text-[11.5px] outline-none focus:border-border-strong"
                />
              </div>
            );
          })}
          <div className="flex flex-wrap justify-end gap-2">
            <Button size="sm" variant="outline" onClick={saveDraft} disabled={pending}>
              {pending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : null}
              {pending ? copy.savingDraft : copy.saveDraft}
            </Button>
            <Button size="sm" onClick={submit} disabled={pending || !requiredComplete()}>
              {pending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <ClipboardCheck className="h-3.5 w-3.5" />}
              {pending ? copy.submitting : copy.submit}
            </Button>
          </div>
        </div>
      ) : assignment.ratings?.length ? (
        <div className="mt-3 grid gap-2 md:grid-cols-2">
          {assignment.ratings.map((rating) => (
            <div key={rating.id} className="rounded-[6px] border border-border bg-bg-elevated px-3 py-2.5">
              <div className="flex items-center justify-between gap-2">
                <span className="text-[11px] font-medium text-text">{rating.criterion_label}</span>
                <span className="font-mono text-[12px] font-semibold text-primary">{rating.rating}/5</span>
              </div>
              {rating.comment ? <p className="mt-1.5 whitespace-pre-wrap text-[10.5px] leading-4 text-text-muted">{rating.comment}</p> : null}
            </div>
          ))}
        </div>
      ) : (
        <p className="mt-3 text-[11px] text-text-faint">{copy.feedbackHidden}</p>
      )}
      {assignment.weighted_average != null ? (
        <p className="mt-3 text-right font-mono text-[10.5px] text-text-muted">
          {copy.average}: {assignment.weighted_average.toFixed(2)}/5
        </p>
      ) : null}
    </article>
  );
}


function DecisionSection({
  candidateId,
  positionId,
  positionTitle,
  currentUserId,
  panel,
  canManage,
  onDecision,
}: {
  candidateId: string;
  positionId: string;
  positionTitle: string | null;
  currentUserId: string;
  panel: ScorecardPanel;
  canManage: boolean;
  onDecision: (decision: HiringDecision) => void;
}) {
  const { m } = useI18n();
  const copy = m.scorecards;
  const decision = panel.decision;
  const [editing, setEditing] = useState(false);
  const [recommendation, setRecommendation] = useState<HiringRecommendation>(decision?.recommendation ?? "advance");
  const [summary, setSummary] = useState(decision?.summary ?? "");
  const [requiredApprovals, setRequiredApprovals] = useState(decision?.required_approvals ?? 1);
  const ownApproval = decision?.approvals.find((approval) => approval.user_id === currentUserId);
  const [voteComment, setVoteComment] = useState(ownApproval?.comment ?? "");
  const [pending, startTransition] = useTransition();
  const approveCount = decision?.approvals.filter((approval) => approval.vote === "approve").length ?? 0;

  useEffect(() => {
    setRecommendation(decision?.recommendation ?? "advance");
    setSummary(decision?.summary ?? "");
    setRequiredApprovals(decision?.required_approvals ?? 1);
    setVoteComment(ownApproval?.comment ?? "");
  }, [decision, ownApproval?.comment]);

  function saveDecision(event: React.FormEvent) {
    event.preventDefault();
    if (pending) return;
    startTransition(async () => {
      const result = await saveHiringDecisionAction(candidateId, {
        position_id: positionId,
        recommendation,
        summary: summary.trim() || null,
        required_approvals: requiredApprovals,
      });
      if (!result.ok) {
        toast.error(copy.saveFailed);
        return;
      }
      onDecision(result.data);
      setEditing(false);
      toast.success(copy.decisionSaved);
    });
  }

  function vote(value: "approve" | "reject") {
    if (!decision || pending) return;
    startTransition(async () => {
      const result = await voteHiringDecisionAction(decision.id, candidateId, {
        vote: value,
        comment: voteComment.trim() || null,
      });
      if (!result.ok) {
        toast.error(copy.saveFailed);
        return;
      }
      onDecision(result.data);
      toast.success(copy.voteSaved);
    });
  }

  return (
    <div className="border-t border-border px-4 py-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h3 className="text-[12px] font-semibold text-text">{copy.decisionTitle}</h3>
          <p className="mt-0.5 text-[10.5px] text-text-faint">{positionTitle}</p>
        </div>
        {canManage && panel.revealed && (!decision || decision.status === "pending") ? (
          <Button size="sm" variant="outline" onClick={() => setEditing((current) => !current)}>
            {editing ? m.common.cancel : decision ? copy.editDecision : copy.createDecision}
          </Button>
        ) : null}
      </div>

      {!panel.revealed ? (
        <p className="mt-3 rounded-[6px] border border-dashed border-border px-3 py-3 text-[11px] text-text-muted">
          {copy.decisionRequiresComplete}
        </p>
      ) : editing ? (
        <form onSubmit={saveDecision} className="mt-3 grid gap-3 rounded-[8px] border border-border bg-bg px-3.5 py-3.5 md:grid-cols-2">
          <Field label={copy.recommendation}>
            <select value={recommendation} onChange={(event) => setRecommendation(event.target.value as HiringRecommendation)} className={controlClass}>
              {(["advance", "hold", "reject", "hire"] as const).map((value) => <option key={value} value={value}>{copy.recommendations[value]}</option>)}
            </select>
          </Field>
          <Field label={copy.requiredApprovals}>
            <input type="number" min={1} max={20} value={requiredApprovals} onChange={(event) => setRequiredApprovals(Math.max(1, Math.min(20, Number(event.target.value))))} className={controlClass} />
          </Field>
          <label className="flex flex-col gap-1.5 md:col-span-2">
            <span className="text-[10.5px] font-medium text-text-muted">{copy.decisionSummary}</span>
            <textarea value={summary} onChange={(event) => setSummary(event.target.value)} rows={3} maxLength={8000} required={recommendation === "reject" || recommendation === "hire"} aria-required={recommendation === "reject" || recommendation === "hire"} placeholder={copy.decisionSummaryPlaceholder} className="rounded-[6px] border border-border bg-bg-elevated px-3 py-2 text-[12px] outline-none focus:border-border-strong" />
          </label>
          <div className="flex justify-end md:col-span-2">
            <Button type="submit" size="sm" disabled={pending}>
              {pending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : null}
              {pending ? copy.savingDecision : copy.saveDecision}
            </Button>
          </div>
        </form>
      ) : decision ? (
        <div className="mt-3 rounded-[8px] border border-border bg-bg px-3.5 py-3.5">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="text-[13px] font-semibold text-text">{copy.recommendations[decision.recommendation]}</span>
            <span className={cn(
              "rounded-[4px] px-2 py-1 text-[10px] font-medium",
              decision.status === "approved" ? "bg-accent-muted text-primary" : decision.status === "rejected" ? "bg-destructive/10 text-destructive" : "bg-bg-active text-text-muted",
            )}>{copy.decisionStatuses[decision.status]}</span>
          </div>
          {decision.summary ? <p className="mt-2 whitespace-pre-wrap text-[11.5px] leading-5 text-text-muted">{decision.summary}</p> : null}
          <p className="mt-2 text-[10.5px] text-text-faint">{copy.approvals(approveCount, decision.required_approvals)}</p>
          {decision.approvals.length ? (
            <div className="mt-3 flex flex-col gap-1.5">
              {decision.approvals.map((approval) => (
                <div key={approval.id} className="rounded-[5px] bg-bg-elevated px-2.5 py-2 text-[10.5px] text-text-muted">
                  <span className="font-medium text-text">{approval.user_email}</span> · {approval.vote === "approve" ? copy.approve : copy.reject}
                  {approval.comment ? <p className="mt-1 text-text-faint">{approval.comment}</p> : null}
                </div>
              ))}
            </div>
          ) : null}
          {canManage && decision.status === "pending" ? (
            <div className="mt-4 border-t border-border pt-3">
              <textarea value={voteComment} onChange={(event) => setVoteComment(event.target.value)} rows={2} maxLength={4000} placeholder={copy.voteCommentPlaceholder} className="block w-full rounded-[6px] border border-border bg-bg-elevated px-3 py-2 text-[11.5px] outline-none focus:border-border-strong" />
              <div className="mt-2 flex flex-wrap justify-end gap-2">
                <Button size="sm" variant="destructive" onClick={() => vote("reject")} disabled={pending}>
                  {copy.reject}
                </Button>
                <Button size="sm" onClick={() => vote("approve")} disabled={pending}>
                  {pending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <ShieldCheck className="h-3.5 w-3.5" />}
                  {pending ? copy.voting : copy.approve}
                </Button>
              </div>
            </div>
          ) : null}
        </div>
      ) : (
        <p className="mt-3 text-[11.5px] text-text-muted">{copy.noDecision}</p>
      )}
    </div>
  );
}


function Summary({ label, value, icon }: { label: string; value: string; icon: React.ReactNode }) {
  return (
    <div className="rounded-[7px] border border-border bg-bg px-3 py-3">
      <div className="flex items-center justify-between gap-2 text-text-faint">
        <span className="text-[9.5px] font-medium uppercase tracking-[0.08em]">{label}</span>
        {icon}
      </div>
      <p className="mt-1.5 text-[11.5px] font-medium text-text">{value}</p>
    </div>
  );
}


function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return <label className="flex min-w-0 flex-col gap-1.5"><span className="text-[10.5px] font-medium text-text-muted">{label}</span>{children}</label>;
}


const controlClass = "min-h-9 w-full rounded-[6px] border border-border bg-bg-elevated px-3 py-2 text-[12px] text-text outline-none focus:border-border-strong";


export { EMPTY_PANEL as EMPTY_SCORECARD_PANEL };
