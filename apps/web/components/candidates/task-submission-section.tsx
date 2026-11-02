"use client";

import { useState, useTransition } from "react";
import {
  ChevronDown,
  ExternalLink,
  FileCode2,
  Plus,
  Sparkles,
  Trash2,
} from "lucide-react";
import { toast } from "sonner";

import {
  createTaskAction,
  deleteTaskAction,
  evaluateTaskAction,
} from "@/lib/actions";
import { useI18n } from "@/components/i18n/locale-provider";
import { SCORE_VAR, scoreTier } from "@/lib/score";
import { cn } from "@/lib/utils";
import type { TaskSubmission } from "@/lib/types";

/** @ui-only Task-submission section composition properties. */
type Props = {
  candidateId: string;
  initial: TaskSubmission[];
  canManage: boolean;
  evaluationAvailable: boolean;
};

/** @ui-only Task-submission editor mode. */
type SubType = "repo" | "text";

/**
 * Technical-task submissions. A recruiter records the task (brief) + the
 * candidate's submission (repo link or pasted solution), then "Evaluate with
 * AI" absorbs it into a score + summary that folds into the candidate's
 * evidence.
 */
export function TaskSubmissionSection({
  candidateId,
  initial,
  canManage,
  evaluationAvailable,
}: Props) {
  const { m } = useI18n();
  const t = m.enrichment.tasks;

  const [items, setItems] = useState<TaskSubmission[]>(initial);
  const [adding, setAdding] = useState(false);
  const [title, setTitle] = useState("");
  const [brief, setBrief] = useState("");
  const [type, setType] = useState<SubType>("repo");
  const [repoUrl, setRepoUrl] = useState("");
  const [content, setContent] = useState("");
  const [openId, setOpenId] = useState<string | null>(null);
  const [evaluatingId, setEvaluatingId] = useState<string | null>(null);
  const [busy, start] = useTransition();

  function create(e: React.FormEvent) {
    e.preventDefault();
    const tt = title.trim();
    if (!tt) return;
    start(async () => {
      const res = await createTaskAction(candidateId, {
        title: tt,
        prompt: brief.trim() || null,
        submission_type: type,
        repo_url: type === "repo" ? repoUrl.trim() || null : null,
        content: type === "text" ? content.trim() || null : null,
      });
      if (!res.ok) {
        toast.error(t.failedToast(res.error));
        return;
      }
      setItems((prev) => [res.data, ...prev]);
      setTitle("");
      setBrief("");
      setRepoUrl("");
      setContent("");
      setAdding(false);
      toast.success(t.createdToast);
    });
  }

  function evaluate(id: string) {
    if (evaluatingId) return;
    setEvaluatingId(id);
    void evaluateTaskAction(id, candidateId).then((res) => {
      setEvaluatingId(null);
      if (!res.ok) {
        toast.error(t.failedToast(res.error));
        return;
      }
      setItems((cur) => cur.map((x) => (x.id === id ? res.data : x)));
      setOpenId(id);
      toast.success(t.evaluatedToast);
    });
  }

  function remove(id: string) {
    if (!window.confirm(t.deleteConfirm)) return;
    const prev = items;
    setItems((cur) => cur.filter((x) => x.id !== id));
    void deleteTaskAction(id, candidateId).then((res) => {
      if (!res.ok) {
        setItems(prev);
        toast.error(t.failedToast(res.error));
      }
    });
  }

  return (
    <div className="rounded-lg border border-border bg-bg-elevated">
      <header className="flex items-center justify-between gap-3 border-b border-border px-5 py-3.5">
        <div className="flex min-w-0 items-center gap-2">
          <FileCode2 className="h-3.5 w-3.5 text-text-muted" />
          <h2 className="text-[13px] font-semibold tracking-tight">{t.title}</h2>
          {items.length > 0 ? (
            <span className="font-mono text-[10px] text-text-faint">
              · {items.length}
            </span>
          ) : null}
        </div>
        {canManage ? (
          <button
            type="button"
            onClick={() => setAdding((v) => !v)}
            className="inline-flex items-center gap-1.5 rounded-[6px] border border-border bg-bg px-2.5 py-1 text-[11px] font-medium text-text transition-colors hover:border-border-strong hover:bg-bg-hover"
          >
            <Plus className="h-3 w-3" />
            {t.add}
          </button>
        ) : null}
      </header>

      {adding && canManage ? (
        <form
          onSubmit={create}
          className="flex flex-col gap-2 border-b border-border p-3"
        >
          <input
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder={t.titlePlaceholder}
            className="block w-full rounded-md border border-border bg-bg px-3 py-2 text-[12.5px] outline-none placeholder:text-text-faint focus:border-border-strong"
          />
          <textarea
            value={brief}
            onChange={(e) => setBrief(e.target.value)}
            rows={2}
            placeholder={t.briefPlaceholder}
            className="block w-full resize-y rounded-md border border-border bg-bg px-3 py-2 text-[12px] leading-[1.55] outline-none placeholder:text-text-faint focus:border-border-strong"
          />
          <div className="flex items-center gap-1 rounded-[6px] border border-border bg-bg p-0.5">
            {(["repo", "text"] as SubType[]).map((opt) => (
              <button
                key={opt}
                type="button"
                onClick={() => setType(opt)}
                className={cn(
                  "flex flex-1 items-center justify-center rounded-[5px] px-2.5 py-1 text-[11.5px] font-medium transition-colors",
                  type === opt
                    ? "bg-bg-elevated text-text"
                    : "text-text-muted hover:text-text",
                )}
              >
                {opt === "repo" ? t.typeRepo : t.typeText}
              </button>
            ))}
          </div>
          {type === "repo" ? (
            <input
              value={repoUrl}
              onChange={(e) => setRepoUrl(e.target.value)}
              placeholder={t.repoPlaceholder}
              className="block w-full rounded-md border border-border bg-bg px-3 py-2 font-mono text-[11.5px] outline-none placeholder:text-text-faint focus:border-border-strong"
            />
          ) : (
            <textarea
              value={content}
              onChange={(e) => setContent(e.target.value)}
              rows={6}
              placeholder={t.contentPlaceholder}
              className="block w-full resize-y rounded-md border border-border bg-bg px-3 py-2 font-mono text-[11.5px] leading-[1.5] outline-none placeholder:text-text-faint focus:border-border-strong"
            />
          )}
          <div className="flex items-center justify-end gap-2">
            <button
              type="button"
              onClick={() => setAdding(false)}
              className="rounded-[6px] px-3 py-1.5 text-[12px] font-medium text-text-muted transition-colors hover:bg-bg-hover hover:text-text"
            >
              {t.cancel}
            </button>
            <button
              type="submit"
              disabled={busy || !title.trim()}
              className="rounded-md bg-text px-3 py-1.5 text-[12px] font-medium text-bg transition-opacity hover:opacity-90 disabled:opacity-40"
            >
              {busy ? t.creating : t.create}
            </button>
          </div>
        </form>
      ) : null}

      {items.length === 0 ? (
        <div className="px-5 py-8 text-center text-[12px] text-text-muted">
          <p>{t.empty}</p>
          <p className="mx-auto mt-1 max-w-sm text-[11px] text-text-faint">
            {t.emptyHint}
          </p>
        </div>
      ) : (
        <ul className="flex flex-col">
          {items.map((task) => (
            <TaskRow
              key={task.id}
              task={task}
              open={openId === task.id}
              evaluating={evaluatingId === task.id}
              disabled={!!evaluatingId}
              canEvaluate={evaluationAvailable}
              canDelete={canManage}
              onToggle={() =>
                setOpenId((cur) => (cur === task.id ? null : task.id))
              }
              onEvaluate={() => evaluate(task.id)}
              onDelete={() => remove(task.id)}
              t={t}
            />
          ))}
        </ul>
      )}
    </div>
  );
}

function StatusPill({ status, label }: { status: string; label: string }) {
  const tone =
    status === "evaluated"
      ? "border-score-high/30 bg-score-high/10 text-score-high"
      : status === "failed"
        ? "border-score-low/30 bg-score-low/10 text-score-low"
        : "border-border bg-bg text-text-muted";
  return (
    <span
      className={cn(
        "inline-flex shrink-0 items-center rounded-full border px-2 py-0.5 text-[10px] font-medium",
        tone,
      )}
    >
      {label}
    </span>
  );
}

function TaskRow({
  task,
  open,
  evaluating,
  disabled,
  canEvaluate,
  canDelete,
  onToggle,
  onEvaluate,
  onDelete,
  t,
}: {
  task: TaskSubmission;
  open: boolean;
  evaluating: boolean;
  disabled: boolean;
  canEvaluate: boolean;
  canDelete: boolean;
  onToggle: () => void;
  onEvaluate: () => void;
  onDelete: () => void;
  t: ReturnType<typeof useI18n>["m"]["enrichment"]["tasks"];
}) {
  const tier = scoreTier(task.ai_score);
  const statusLabel =
    (t.status as Record<string, string>)[task.status] ?? task.status;
  const ev = task.evaluation;
  const recLabel = ev?.recommendation
    ? ((t.recommendationValues as Record<string, string>)[ev.recommendation] ??
      ev.recommendation)
    : null;

  return (
    <li className="border-b border-border last:border-b-0">
      <div className="flex items-center gap-3 px-5 py-2.5">
        <button
          type="button"
          onClick={onToggle}
          className="flex min-w-0 flex-1 items-center gap-2 text-left"
        >
          <ChevronDown
            className={cn(
              "h-3 w-3 shrink-0 text-text-faint transition-transform",
              open && "rotate-180",
            )}
          />
          <span className="truncate text-[12.5px] font-medium text-text">
            {task.title}
          </span>
          <StatusPill status={task.status} label={statusLabel} />
        </button>
        {task.ai_score != null ? (
          <span
            className="font-mono text-[14px] font-semibold tabular-nums"
            style={{ color: tier != null ? SCORE_VAR[tier] : "var(--text-faint)" }}
            aria-label={`${t.aiScore}: ${task.ai_score}`}
            title={t.aiScore}
          >
            {task.ai_score}
          </span>
        ) : null}
        {canEvaluate ? (
          <button
            type="button"
            onClick={onEvaluate}
            disabled={disabled}
            className="inline-flex shrink-0 items-center gap-1.5 rounded-[6px] border border-border bg-bg px-2 py-1 text-[11px] font-medium text-text-muted transition-colors hover:border-border-strong hover:bg-bg-hover hover:text-text disabled:opacity-40"
          >
            <Sparkles className="h-3 w-3" />
            {evaluating
              ? t.evaluating
              : task.status === "evaluated"
                ? t.reevaluate
                : t.evaluate}
          </button>
        ) : null}
        {canDelete ? (
          <button
            type="button"
            onClick={onDelete}
            className="inline-flex shrink-0 items-center rounded px-1.5 py-1 text-text-faint transition-colors hover:bg-score-low/10 hover:text-score-low"
            aria-label={t.delete}
          >
            <Trash2 className="h-3 w-3" />
          </button>
        ) : null}
      </div>

      {open ? (
        <div className="flex flex-col gap-3 px-5 pb-4 pl-10">
          {task.repo_url ? (
            <a
              href={task.repo_url}
              target="_blank"
              rel="noreferrer"
              className="inline-flex w-fit items-center gap-1 font-mono text-[11px] text-text-muted transition-colors hover:text-text"
            >
              <ExternalLink className="h-2.5 w-2.5" />
              {t.viewRepo}
            </a>
          ) : null}

          {task.prompt ? (
            <div>
              <div className="mb-1 text-[10px] font-medium uppercase tracking-[0.08em] text-text-faint">
                {t.brief}
              </div>
              <p className="whitespace-pre-wrap text-[12.5px] leading-[1.55] text-text-muted">
                {task.prompt}
              </p>
            </div>
          ) : null}

          {task.ai_summary ? (
            <p className="text-[12.5px] leading-[1.55] text-text">
              {task.ai_summary}
            </p>
          ) : null}

          {ev && (ev.strengths?.length || ev.concerns?.length) ? (
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              {ev.strengths && ev.strengths.length > 0 ? (
                <div>
                  <div className="mb-1 text-[10px] font-medium uppercase tracking-[0.08em] text-score-high">
                    {t.strengths}
                  </div>
                  <ul className="flex flex-col gap-0.5">
                    {ev.strengths.map((s, i) => (
                      <li key={i} className="text-[12px] leading-[1.5] text-text-muted">
                        + {s}
                      </li>
                    ))}
                  </ul>
                </div>
              ) : null}
              {ev.concerns && ev.concerns.length > 0 ? (
                <div>
                  <div className="mb-1 text-[10px] font-medium uppercase tracking-[0.08em] text-score-low">
                    {t.concerns}
                  </div>
                  <ul className="flex flex-col gap-0.5">
                    {ev.concerns.map((c, i) => (
                      <li key={i} className="text-[12px] leading-[1.5] text-text-muted">
                        − {c}
                      </li>
                    ))}
                  </ul>
                </div>
              ) : null}
            </div>
          ) : null}

          {recLabel ? (
            <div className="flex items-center gap-2 text-[11.5px]">
              <span className="font-medium uppercase tracking-[0.08em] text-text-faint">
                {t.recommendation}
              </span>
              <span className="font-semibold text-text">{recLabel}</span>
            </div>
          ) : null}
        </div>
      ) : null}
    </li>
  );
}
