"use client";

import { useEffect, useMemo, useState, useTransition } from "react";
import {
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  ClipboardList,
  Plus,
  Trash2,
  X,
} from "lucide-react";
import { toast } from "sonner";

import {
  completeInterviewAction,
  createInterviewKitVersionAction,
  createManualInterviewAction,
  deleteInterviewAction,
  getInterviewAction,
  scoreQuestionAction,
  updateInterviewAction,
} from "@/lib/actions";
import { useI18n } from "@/components/i18n/locale-provider";
import type { ApiResponseSchema } from "@/lib/generated/contracts";
import { cn } from "@/lib/utils";
import type {
  AssignableUser,
  Interview,
  InterviewDetail,
  InterviewQuestion,
  InterviewQuestionDraft,
} from "@/lib/types";

type PositionOption = Pick<ApiResponseSchema<"PositionOut">, "id" | "title"> & {
  applicationId: ApiResponseSchema<"ApplicationOut">["id"];
};

/** @ui-only Interview section composition properties. */
type Props = {
  candidateId: string;
  initialInterviews: Interview[];
  positions: PositionOption[];
  assignableUsers: AssignableUser[];
  currentUserId: string;
  defaultPositionId?: string | null;
  canManage: boolean;
  canSubmitFeedback: boolean;
};

const STATUS_TONE: Record<string, { color: string; bg: string }> = {
  draft: { color: "var(--text-muted)", bg: "var(--bg-active)" },
  in_progress: { color: "var(--accent)", bg: "var(--accent-muted)" },
  scored: { color: "var(--score-high)", bg: "oklch(0.7 0.15 162 / 0.12)" },
  archived: { color: "var(--text-faint)", bg: "var(--bg-active)" },
};

export function InterviewSection({
  candidateId,
  initialInterviews,
  positions,
  assignableUsers,
  currentUserId,
  defaultPositionId,
  canManage,
  canSubmitFeedback,
}: Props) {
  const { m } = useI18n();
  const [interviews, setInterviews] = useState<Interview[]>(initialInterviews);
  const [openId, setOpenId] = useState<string | null>(initialInterviews[0]?.id ?? null);
  const [showCreate, setShowCreate] = useState<boolean>(
    canManage && initialInterviews.length === 0,
  );

  function onDelete(id: string) {
    if (!window.confirm(m.interview.deleteConfirm)) return;
    void deleteInterviewAction(id, candidateId).then((res) => {
      if (!res.ok) {
        toast.error(m.interview.failedToast(res.error));
        return;
      }
      setInterviews((prev) => prev.filter((iv) => iv.id !== id));
      if (openId === id) setOpenId(null);
    });
  }

  function onInterviewUpdate(updated: Interview) {
    setInterviews((prev) =>
      prev.map((iv) => (iv.id === updated.id ? { ...iv, ...updated } : iv)),
    );
  }

  return (
    <div className="rounded-lg border border-border bg-bg-elevated">
      <header className="flex items-center justify-between gap-3 border-b border-border px-5 py-3.5">
        <div className="flex items-center gap-2">
          <ClipboardList className="h-3.5 w-3.5 text-text-muted" />
          <h2 className="text-[13px] font-semibold tracking-tight">
            {m.interview.sectionTitle}
          </h2>
          {interviews.length > 0 ? (
            <span className="font-mono text-[10px] text-text-faint">
              · {interviews.length}
            </span>
          ) : null}
        </div>
        {canManage && !showCreate ? (
          <button
            type="button"
            onClick={() => setShowCreate(true)}
            disabled={positions.length === 0}
            className="inline-flex items-center gap-1.5 rounded-[6px] border border-border bg-bg px-2.5 py-1 text-[11px] font-medium text-text transition-colors hover:border-border-strong hover:bg-bg-hover disabled:opacity-40"
          >
            <Plus className="h-3 w-3" />
            {m.interview.createKit}
          </button>
        ) : null}
      </header>

      {canManage && showCreate ? (
        <ManualInterviewForm
          candidateId={candidateId}
          positions={positions}
          assignableUsers={assignableUsers}
          currentUserId={currentUserId}
          defaultPositionId={defaultPositionId}
          onCancel={() => setShowCreate(false)}
          onCreated={(detail) => {
            setInterviews((current) => [
              shallowOf(detail),
              ...current.filter((item) => item.id !== detail.id),
            ]);
            setOpenId(detail.id);
            setShowCreate(false);
          }}
        />
      ) : null}

      {interviews.length === 0 && !showCreate ? (
        <div className="px-5 py-10 text-center">
          <p className="mb-1 text-[13px] font-medium text-text">
            {m.interview.noInterviewsTitle}
          </p>
          <p className="text-[12px] text-text-muted">
            {m.interview.noInterviewsDesc}
          </p>
        </div>
      ) : (
        <ul className="flex flex-col">
          {interviews.map((iv) => (
            <InterviewRow
              key={iv.id}
              interview={iv}
              open={openId === iv.id}
              onToggle={() => setOpenId(openId === iv.id ? null : iv.id)}
              onDelete={() => onDelete(iv.id)}
              canManage={canManage}
              canSubmitFeedback={canSubmitFeedback}
              positionTitle={positions.find((p) => p.id === iv.position_id)?.title}
              onUpdate={onInterviewUpdate}
            />
          ))}
        </ul>
      )}
    </div>
  );
}

const EMPTY_QUESTION: InterviewQuestionDraft = {
  category: "technical",
  question_text: "",
  rationale: null,
  expected_signals: null,
  is_required: true,
};

function ManualInterviewForm({
  candidateId,
  positions,
  assignableUsers,
  currentUserId,
  defaultPositionId,
  onCancel,
  onCreated,
}: {
  candidateId: string;
  positions: PositionOption[];
  assignableUsers: AssignableUser[];
  currentUserId: string;
  defaultPositionId?: string | null;
  onCancel: () => void;
  onCreated: (detail: InterviewDetail) => void;
}) {
  const { m } = useI18n();
  const initialPosition = positions.some((item) => item.id === defaultPositionId)
    ? (defaultPositionId ?? "")
    : (positions[0]?.id ?? "");
  const initialInterviewer = assignableUsers.some(
    (user) => user.id === currentUserId,
  )
    ? currentUserId
    : (assignableUsers[0]?.id ?? "");
  const [positionId, setPositionId] = useState(initialPosition);
  const [interviewerIds, setInterviewerIds] = useState<string[]>(
    initialInterviewer ? [initialInterviewer] : [],
  );
  const [questions, setQuestions] = useState<InterviewQuestionDraft[]>([
    { ...EMPTY_QUESTION },
  ]);
  const [notes, setNotes] = useState("");
  const [pending, startTransition] = useTransition();

  const selectedPosition = positions.find((item) => item.id === positionId);
  const valid =
    selectedPosition != null &&
    interviewerIds.length > 0 &&
    questions.length > 0 &&
    questions.every((question) => question.question_text.trim().length > 0);

  function toggleInterviewer(userId: string) {
    setInterviewerIds((current) =>
      current.includes(userId)
        ? current.filter((id) => id !== userId)
        : [...current, userId],
    );
  }

  function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!selectedPosition || !valid || pending) return;
    startTransition(async () => {
      const result = await createManualInterviewAction(candidateId, {
        application_id: selectedPosition.applicationId,
        position_id: selectedPosition.id,
        interviewer_user_ids: interviewerIds,
        questions: questions.map((question) => ({
          ...question,
          question_text: question.question_text.trim(),
          rationale: question.rationale?.trim() || null,
          expected_signals: question.expected_signals?.trim() || null,
        })),
        notes: notes.trim() || null,
      });
      if (!result.ok) {
        toast.error(m.interview.failedToast(result.error));
        return;
      }
      toast.success(m.interview.kitCreated);
      onCreated(result.data);
    });
  }

  return (
    <form
      onSubmit={submit}
      className="border-b border-border bg-bg-hover/40 px-5 py-4"
    >
      <div className="mb-4 flex items-start justify-between gap-4">
        <div>
          <h3 className="text-[12.5px] font-semibold text-text">
            {m.interview.createKit}
          </h3>
          <p className="mt-1 text-[11.5px] text-text-muted">
            {m.interview.createKitDescription}
          </p>
        </div>
        <button
          type="button"
          onClick={onCancel}
          aria-label={m.common.close}
          className="rounded p-1 text-text-faint hover:bg-bg-hover hover:text-text"
        >
          <X className="h-3.5 w-3.5" />
        </button>
      </div>

      {positions.length === 0 ? (
        <p className="text-[12px] italic text-text-faint">
          {m.interview.noActiveApplications}
        </p>
      ) : (
        <>
          <label className="mb-4 flex max-w-lg flex-col gap-1.5">
            <span className="text-[10px] font-medium uppercase tracking-[0.08em] text-text-faint">
              {m.interview.pickApplication}
            </span>
            <select
              value={positionId}
              onChange={(event) => setPositionId(event.target.value)}
              className="rounded-[6px] border border-border bg-bg-elevated px-2.5 py-2 text-[12px] outline-none focus:border-border-strong"
            >
              {positions.map((position) => (
                <option key={position.applicationId} value={position.id}>
                  {position.title}
                </option>
              ))}
            </select>
          </label>

          <fieldset className="mb-4">
            <legend className="mb-2 text-[10px] font-medium uppercase tracking-[0.08em] text-text-faint">
              {m.interview.assignInterviewers}
            </legend>
            {assignableUsers.length === 0 ? (
              <p className="text-[12px] text-score-low">
                {m.interview.noAssignableInterviewers}
              </p>
            ) : (
              <div className="flex flex-wrap gap-2">
                {assignableUsers.map((user) => (
                  <label
                    key={user.id}
                    className="inline-flex cursor-pointer items-center gap-2 rounded-md border border-border bg-bg-elevated px-2.5 py-1.5 text-[11.5px] text-text"
                  >
                    <input
                      type="checkbox"
                      checked={interviewerIds.includes(user.id)}
                      onChange={() => toggleInterviewer(user.id)}
                    />
                    {user.email}
                  </label>
                ))}
              </div>
            )}
          </fieldset>

          <QuestionsEditor questions={questions} onChange={setQuestions} />

          <label className="mt-4 flex flex-col gap-1.5">
            <span className="text-[10px] font-medium uppercase tracking-[0.08em] text-text-faint">
              {m.interview.kitNotes}
            </span>
            <textarea
              value={notes}
              onChange={(event) => setNotes(event.target.value)}
              rows={2}
              className="resize-y rounded-[6px] border border-border bg-bg-elevated px-3 py-2 text-[12px] outline-none focus:border-border-strong"
              placeholder={m.interview.kitNotesPlaceholder}
            />
          </label>

          <div className="mt-4 flex items-center gap-2">
            <button
              type="submit"
              disabled={!valid || pending}
              className="inline-flex items-center gap-1.5 rounded-[6px] bg-text px-3 py-1.5 text-[12px] font-medium text-bg disabled:opacity-40"
            >
              <Plus className="h-3 w-3" />
              {pending ? m.interview.savingKit : m.interview.saveKit}
            </button>
            <button
              type="button"
              onClick={onCancel}
              className="rounded-[6px] px-3 py-1.5 text-[12px] text-text-muted hover:bg-bg-hover"
            >
              {m.common.cancel}
            </button>
          </div>
        </>
      )}
    </form>
  );
}

function QuestionsEditor({
  questions,
  onChange,
}: {
  questions: InterviewQuestionDraft[];
  onChange: (questions: InterviewQuestionDraft[]) => void;
}) {
  const { m } = useI18n();

  function update(index: number, patch: Partial<InterviewQuestionDraft>) {
    onChange(
      questions.map((question, questionIndex) =>
        questionIndex === index ? { ...question, ...patch } : question,
      ),
    );
  }

  return (
    <fieldset>
      <div className="mb-2 flex items-center justify-between gap-3">
        <legend className="text-[10px] font-medium uppercase tracking-[0.08em] text-text-faint">
          {m.interview.questions}
        </legend>
        <button
          type="button"
          onClick={() => onChange([...questions, { ...EMPTY_QUESTION }])}
          disabled={questions.length >= 50}
          className="inline-flex items-center gap-1 text-[11px] font-medium text-text-muted hover:text-text disabled:opacity-40"
        >
          <Plus className="h-3 w-3" />
          {m.interview.addQuestion}
        </button>
      </div>
      <div className="space-y-3">
        {questions.map((question, index) => (
          <div
            key={index}
            className="rounded-lg border border-border bg-bg-elevated p-3"
          >
            <div className="mb-2 flex items-center gap-2">
              <select
                value={question.category}
                onChange={(event) =>
                  update(index, {
                    category: event.target
                      .value as InterviewQuestionDraft["category"],
                  })
                }
                aria-label={m.interview.questionCategory}
                className="rounded-[5px] border border-border bg-bg px-2 py-1 text-[11px]"
              >
                {(["technical", "behavioral", "situational", "culture"] as const).map(
                  (category) => (
                    <option key={category} value={category}>
                      {(m.interview.categories as Record<string, string>)[category]}
                    </option>
                  ),
                )}
              </select>
              <label className="ml-auto inline-flex items-center gap-1.5 text-[11px] text-text-muted">
                <input
                  type="checkbox"
                  checked={question.is_required !== false}
                  onChange={(event) =>
                    update(index, { is_required: event.target.checked })
                  }
                />
                {m.interview.requiredQuestion}
              </label>
              <button
                type="button"
                onClick={() =>
                  onChange(questions.filter((_, questionIndex) => questionIndex !== index))
                }
                disabled={questions.length === 1}
                aria-label={m.interview.removeQuestion}
                className="rounded p-1 text-text-faint hover:bg-bg-hover hover:text-score-low disabled:opacity-30"
              >
                <Trash2 className="h-3 w-3" />
              </button>
            </div>
            <textarea
              value={question.question_text}
              onChange={(event) =>
                update(index, { question_text: event.target.value })
              }
              required
              rows={2}
              maxLength={10_000}
              placeholder={m.interview.questionPlaceholder}
              className="block w-full resize-y rounded-[6px] border border-border bg-bg px-3 py-2 text-[12.5px] outline-none focus:border-border-strong"
            />
            <div className="mt-2 grid gap-2 md:grid-cols-2">
              <input
                value={question.rationale ?? ""}
                onChange={(event) => update(index, { rationale: event.target.value })}
                maxLength={4_000}
                placeholder={m.interview.rationalePlaceholder}
                className="rounded-[6px] border border-border bg-bg px-3 py-2 text-[11.5px] outline-none focus:border-border-strong"
              />
              <input
                value={question.expected_signals ?? ""}
                onChange={(event) =>
                  update(index, { expected_signals: event.target.value })
                }
                maxLength={4_000}
                placeholder={m.interview.signalsPlaceholder}
                className="rounded-[6px] border border-border bg-bg px-3 py-2 text-[11.5px] outline-none focus:border-border-strong"
              />
            </div>
          </div>
        ))}
      </div>
    </fieldset>
  );
}

function KitRevisionForm({
  interview,
  onCancel,
  onUpdated,
}: {
  interview: InterviewDetail;
  onCancel: () => void;
  onUpdated: (detail: InterviewDetail) => void;
}) {
  const { m } = useI18n();
  const [questions, setQuestions] = useState<InterviewQuestionDraft[]>(
    interview.questions.map((question) => ({
      category: question.category as InterviewQuestionDraft["category"],
      question_text: question.question_text,
      rationale: question.rationale,
      expected_signals: question.expected_signals,
      is_required: question.is_required,
    })),
  );
  const [pending, startTransition] = useTransition();
  const valid =
    questions.length > 0 &&
    questions.every((question) => question.question_text.trim().length > 0);

  function save(event: React.FormEvent) {
    event.preventDefault();
    if (!valid || pending) return;
    startTransition(async () => {
      const result = await createInterviewKitVersionAction(interview.id, {
        expected_version: interview.kit_version,
        questions: questions.map((question) => ({
          ...question,
          question_text: question.question_text.trim(),
          rationale: question.rationale?.trim() || null,
          expected_signals: question.expected_signals?.trim() || null,
        })),
      });
      if (!result.ok) {
        toast.error(m.interview.failedToast(result.error));
        return;
      }
      toast.success(m.interview.kitRevised);
      onUpdated(result.data);
    });
  }

  return (
    <form
      onSubmit={save}
      className="mb-5 rounded-lg border border-border bg-bg-elevated p-4"
    >
      <p className="mb-3 text-[11.5px] text-text-muted">
        {m.interview.reviseKitDescription}
      </p>
      <QuestionsEditor questions={questions} onChange={setQuestions} />
      <div className="mt-4 flex items-center gap-2">
        <button
          type="submit"
          disabled={!valid || pending}
          className="rounded-[6px] bg-text px-3 py-1.5 text-[12px] font-medium text-bg disabled:opacity-40"
        >
          {pending ? m.interview.savingKit : m.interview.saveRevision}
        </button>
        <button
          type="button"
          onClick={onCancel}
          className="rounded-[6px] px-3 py-1.5 text-[12px] text-text-muted hover:bg-bg-hover"
        >
          {m.common.cancel}
        </button>
      </div>
    </form>
  );
}

function shallowOf(detail: InterviewDetail): Interview {
  const { questions: _q, ...rest } = detail;
  void _q;
  return rest;
}

function InterviewRow({
  interview,
  open,
  onToggle,
  onDelete,
  canManage,
  canSubmitFeedback,
  positionTitle,
  onUpdate,
}: {
  interview: Interview;
  open: boolean;
  onToggle: () => void;
  onDelete: () => void;
  canManage: boolean;
  canSubmitFeedback: boolean;
  positionTitle: string | undefined;
  onUpdate: (iv: Interview) => void;
}) {
  const { m } = useI18n();
  const tone =
    STATUS_TONE[interview.status] ?? STATUS_TONE.draft;
  const statusLabel = (
    {
      draft: m.interview.statusDraft,
      in_progress: m.interview.statusInProgress,
      scored: m.interview.statusScored,
      archived: m.interview.statusArchived,
    } as Record<string, string>
  )[interview.status];

  return (
    <li className="border-b border-border last:border-b-0">
      <div className="flex items-center gap-3 px-5 py-3">
        <button
          type="button"
          onClick={onToggle}
          className="flex flex-1 min-w-0 items-center gap-2 text-left"
        >
          {open ? (
            <ChevronDown className="h-3 w-3 shrink-0 text-text-muted" />
          ) : (
            <ChevronRight className="h-3 w-3 shrink-0 text-text-muted" />
          )}
          <span className="truncate text-[12.5px] font-medium text-text">
            {positionTitle ?? "Position"}
          </span>
          <span
            className="rounded-[3px] px-1.5 py-[2px] font-mono text-[10px] lowercase tracking-wide"
            style={{ color: tone.color, background: tone.bg }}
          >
            {statusLabel}
          </span>
          <span className="font-mono text-[10px] text-text-faint">
            {m.interview.questionCountSummary(interview.question_count)}
          </span>
        </button>
        <span className="font-mono text-[12px] tabular-nums text-text-muted">
          {interview.overall_score != null
            ? interview.overall_score.toFixed(1)
            : "—"}
        </span>
        {canManage ? (
          <button
            type="button"
            onClick={onDelete}
            title={m.interview.delete}
            className="flex h-6 w-6 items-center justify-center rounded-[4px] text-text-faint transition-colors hover:bg-bg-hover hover:text-score-low"
          >
            <Trash2 className="h-3 w-3" />
          </button>
        ) : null}
      </div>
      {open ? (
        <InterviewBody
          interview={interview}
          onUpdate={onUpdate}
          canManage={canManage}
          canSubmitFeedback={canSubmitFeedback}
        />
      ) : null}
    </li>
  );
}

function InterviewBody({
  interview,
  onUpdate,
  canManage,
  canSubmitFeedback,
}: {
  interview: Interview;
  onUpdate: (iv: Interview) => void;
  canManage: boolean;
  canSubmitFeedback: boolean;
}) {
  const { m } = useI18n();
  const [detail, setDetail] = useState<InterviewDetail | null>(null);
  const [questions, setQuestions] = useState<InterviewQuestion[] | null>(null);
  const [overallNotes, setOverallNotes] = useState<string>(
    interview.notes ?? "",
  );
  const [loading, setLoading] = useState(true);
  const [loadFailed, setLoadFailed] = useState(false);
  const [editingKit, setEditingKit] = useState(false);
  const [submitting, startSubmitting] = useTransition();

  async function loadDetail(cancelled?: () => boolean) {
    setLoading(true);
    const result = await getInterviewAction(interview.id);
    if (cancelled?.()) return;
    if (result.ok) {
      setDetail(result.data);
      setQuestions(result.data.questions);
      setOverallNotes(result.data.notes ?? "");
      setLoadFailed(false);
      onUpdate(shallowOf(result.data));
    } else {
      setLoadFailed(true);
    }
    setLoading(false);
  }

  useEffect(() => {
    let cancelled = false;
    void loadDetail(() => cancelled);
    return () => {
      cancelled = true;
    };
    // The interview id is the stable identity of this editor. Parent summary
    // refreshes must not wipe locally-entered feedback.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [interview.id]);

  function onScore(qid: string, patch: Partial<InterviewQuestion>) {
    setQuestions((prev) =>
      (prev ?? []).map((q) => (q.id === qid ? { ...q, ...patch } : q)),
    );
  }

  function onOverallChange(notes: string) {
    setOverallNotes(notes);
    if (!canManage || ["scored", "archived"].includes(interview.status)) return;
    void updateInterviewAction(interview.id, { notes }).then((res) => {
      if (!res.ok) toast.error(m.interview.failedToast(res.error));
      else onUpdate(res.data);
    });
  }

  function completeFeedback() {
    if (submitting) return;
    startSubmitting(async () => {
      const result = await completeInterviewAction(interview.id, {
        notes: overallNotes.trim() || null,
      });
      if (!result.ok) {
        toast.error(m.interview.failedToast(result.error));
        return;
      }
      onUpdate(result.data);
      setDetail((current) =>
        current ? { ...current, ...result.data } : current,
      );
      toast.success(m.interview.feedbackSubmitted);
    });
  }

  const grouped = useMemo(() => {
    const order = ["technical", "behavioral", "situational", "culture"];
    const buckets: Record<string, InterviewQuestion[]> = {};
    for (const q of questions ?? []) {
      (buckets[q.category] ??= []).push(q);
    }
    return order
      .filter((cat) => buckets[cat]?.length)
      .map((cat) => ({ category: cat, items: buckets[cat] }));
  }, [questions]);

  const scored = (questions ?? []).filter((q) => q.score != null).length;
  const status = detail?.status ?? interview.status;
  const feedbackLocked = status === "scored" || status === "archived";

  return (
    <div className="border-t border-border bg-bg/40 px-5 py-4">
      {loading ? (
        <div className="text-center text-[12px] text-text-faint">…</div>
      ) : loadFailed ? (
        <div role="alert" className="flex items-center justify-between gap-3 text-[12px] text-score-low">
          <span>{m.interview.loadFailed}</span>
          <button
            type="button"
            onClick={() => void loadDetail()}
            className="rounded border border-border px-2 py-1 text-text-muted hover:text-text"
          >
            {m.common.retry}
          </button>
        </div>
      ) : (
        <>
          <div className="mb-4 flex items-center justify-between">
            <div className="flex items-center gap-3">
              <span className="font-mono text-[10px] uppercase tracking-[0.08em] text-text-faint">
                {m.interview.scoredCountSummary(scored, questions?.length ?? 0)}
              </span>
              {detail ? (
                <span className="font-mono text-[10px] text-text-faint">
                  {m.interview.kitVersion(detail.kit_version)}
                </span>
              ) : null}
              {canManage && detail?.status === "draft" && scored === 0 ? (
                <button
                  type="button"
                  onClick={() => setEditingKit((current) => !current)}
                  className="text-[11px] font-medium text-text-muted hover:text-text"
                >
                  {editingKit ? m.common.cancel : m.interview.reviseKit}
                </button>
              ) : null}
            </div>
            <div className="flex items-center gap-2">
              <span className="text-[10px] font-medium uppercase tracking-[0.08em] text-text-faint">
                {m.interview.overall}
              </span>
              <span className="font-mono text-[16px] font-semibold tabular-nums text-text">
                {interview.overall_score != null
                  ? interview.overall_score.toFixed(1)
                  : "—"}
              </span>
            </div>
          </div>

          {editingKit && detail ? (
            <KitRevisionForm
              interview={detail}
              onCancel={() => setEditingKit(false)}
              onUpdated={(updated) => {
                setDetail(updated);
                setQuestions(updated.questions);
                onUpdate(shallowOf(updated));
                setEditingKit(false);
              }}
            />
          ) : null}

          {!editingKit ? grouped.map(({ category, items }) => (
            <div key={category} className="mb-5 last:mb-0">
              <div className="mb-2 text-[10px] font-medium uppercase tracking-[0.08em] text-text-faint">
                {(m.interview.categories as Record<string, string>)[category] ??
                  category}
              </div>
              <ul className="flex flex-col gap-3">
                {items.map((q) => (
                  <QuestionCard
                    key={q.id}
                    interviewId={interview.id}
                    question={q}
                    onLocalChange={(patch) => onScore(q.id, patch)}
                    onSaved={() => void loadDetail()}
                    canSubmit={canSubmitFeedback && !feedbackLocked}
                  />
                ))}
              </ul>
            </div>
          )) : null}

          {!editingKit ? <div className="mt-5 border-t border-border pt-4">
            <div className="mb-1.5 text-[10px] font-medium uppercase tracking-[0.08em] text-text-faint">
              {m.interview.overall}
            </div>
            <textarea
              value={overallNotes}
              onChange={(e) => setOverallNotes(e.target.value)}
              onBlur={(e) => onOverallChange(e.target.value)}
              rows={3}
              placeholder={m.interview.overallNotesPlaceholder}
              readOnly={!canSubmitFeedback || feedbackLocked}
              className="block w-full resize-y rounded-[6px] border border-border bg-bg-elevated px-3 py-2 text-[13px] leading-[1.55] outline-none placeholder:text-text-faint focus:border-border-strong"
            />
            {canSubmitFeedback && !feedbackLocked ? (
              <div className="mt-3 flex justify-end">
                <button
                  type="button"
                  onClick={completeFeedback}
                  disabled={submitting}
                  className="inline-flex items-center gap-1.5 rounded-[6px] bg-text px-3 py-1.5 text-[12px] font-medium text-bg disabled:opacity-40"
                >
                  <CheckCircle2 className="h-3.5 w-3.5" />
                  {submitting
                    ? m.interview.submittingFeedback
                    : m.interview.submitFeedback}
                </button>
              </div>
            ) : null}
          </div> : null}
        </>
      )}
    </div>
  );
}

function QuestionCard({
  interviewId,
  question,
  onLocalChange,
  onSaved,
  canSubmit,
}: {
  interviewId: string;
  question: InterviewQuestion;
  onLocalChange: (patch: Partial<InterviewQuestion>) => void;
  onSaved: () => void;
  canSubmit: boolean;
}) {
  const { m } = useI18n();
  const [notes, setNotes] = useState<string>(question.notes ?? "");
  const [showWhy, setShowWhy] = useState(false);

  function setScore(score: number | null) {
    if (!canSubmit) return;
    onLocalChange({ score });
    void scoreQuestionAction(interviewId, question.id, { score }).then((res) => {
      if (!res.ok) toast.error(m.interview.failedToast(res.error));
      else onSaved();
    });
  }

  function saveNotes() {
    if (!canSubmit) return;
    if ((notes ?? "") === (question.notes ?? "")) return;
    onLocalChange({ notes });
    void scoreQuestionAction(interviewId, question.id, { notes }).then((res) => {
      if (!res.ok) toast.error(m.interview.failedToast(res.error));
      else onSaved();
    });
  }

  return (
    <li className="rounded-lg border border-border bg-bg-elevated px-4 py-3">
      <p className="text-[13.5px] leading-[1.5] text-text">
        {question.question_text}
      </p>

      {(question.rationale || question.expected_signals) && (
        <button
          type="button"
          onClick={() => setShowWhy((s) => !s)}
          className="mt-2 inline-flex items-center gap-1 text-[10.5px] uppercase tracking-[0.06em] text-text-faint transition-colors hover:text-text-muted"
        >
          {showWhy ? "▾" : "▸"} {m.interview.rationale}
        </button>
      )}
      {showWhy ? (
        <div className="mt-2 flex flex-col gap-1.5 rounded-[6px] bg-bg/60 px-3 py-2 text-[12px] leading-[1.55] text-text-muted">
          {question.rationale ? (
            <div>
              <span className="font-mono text-[10px] uppercase tracking-[0.06em] text-text-faint">
                {m.interview.rationale}
              </span>{" "}
              {question.rationale}
            </div>
          ) : null}
          {question.expected_signals ? (
            <div>
              <span className="font-mono text-[10px] uppercase tracking-[0.06em] text-text-faint">
                {m.interview.expectedSignals}
              </span>{" "}
              {question.expected_signals}
            </div>
          ) : null}
        </div>
      ) : null}

      <div className="mt-3 flex items-center gap-2">
        <span className="text-[10px] font-medium uppercase tracking-[0.08em] text-text-faint">
          {m.interview.rubricLabel}
        </span>
        <div className="flex items-center gap-1">
          {[1, 2, 3, 4, 5].map((n) => {
            const active = question.score === n;
            return (
              <button
                key={n}
                type="button"
                onClick={() => setScore(active ? null : n)}
                disabled={!canSubmit}
                aria-pressed={active}
                title={`${n}`}
                className={cn(
                  "flex h-6 w-6 items-center justify-center rounded-[4px] border font-mono text-[11px] tabular-nums transition-colors",
                  active
                    ? "border-primary bg-accent-muted text-primary"
                    : "border-border bg-bg text-text-muted hover:border-border-strong hover:text-text",
                )}
              >
                {n}
              </button>
            );
          })}
        </div>
        <span className="font-mono text-[10px] text-text-faint">
          {m.interview.rubricHint}
        </span>
      </div>

      <textarea
        value={notes}
        onChange={(e) => setNotes(e.target.value)}
        onBlur={saveNotes}
        rows={2}
        placeholder={m.interview.notesPlaceholder}
        readOnly={!canSubmit}
        className="mt-2 block w-full resize-y rounded-[6px] border border-border bg-bg px-3 py-2 text-[12.5px] leading-[1.55] outline-none placeholder:text-text-faint focus:border-border-strong"
      />
    </li>
  );
}
