"use client";

import { useState, useTransition } from "react";
import {
  ChevronDown,
  ExternalLink,
  Mic,
  Plus,
  Trash2,
} from "lucide-react";
import { toast } from "sonner";

import {
  createRecordingAction,
  deleteRecordingAction,
} from "@/lib/actions";
import { useI18n } from "@/components/i18n/locale-provider";
import { SCORE_VAR, scoreTier } from "@/lib/score";
import { cn } from "@/lib/utils";
import type { InterviewRecording } from "@/lib/types";

/** @ui-only Recording section composition properties. */
type Props = {
  candidateId: string;
  initial: InterviewRecording[];
  canManage: boolean;
};

/**
 * Interview recordings. Recruiters create a row (title + optional media link);
 * the external record→diarize→transcribe→score pipeline fills in the transcript,
 * diarization and interview score via PATCH. We display whatever is present.
 */
export function RecordingsSection({ candidateId, initial, canManage }: Props) {
  const { m } = useI18n();
  const t = m.enrichment.recordings;

  const [items, setItems] = useState<InterviewRecording[]>(initial);
  const [adding, setAdding] = useState(false);
  const [title, setTitle] = useState("");
  const [url, setUrl] = useState("");
  const [openId, setOpenId] = useState<string | null>(null);
  const [busy, start] = useTransition();

  function create(e: React.FormEvent) {
    e.preventDefault();
    const tt = title.trim();
    if (!tt) return;
    start(async () => {
      const res = await createRecordingAction(candidateId, {
        title: tt,
        media_url: url.trim() || null,
      });
      if (!res.ok) {
        toast.error(t.failedToast(res.error));
        return;
      }
      setItems((prev) => [res.data, ...prev]);
      setTitle("");
      setUrl("");
      setAdding(false);
      toast.success(t.createdToast);
    });
  }

  function remove(id: string) {
    if (!window.confirm(t.deleteConfirm)) return;
    const prev = items;
    setItems((cur) => cur.filter((r) => r.id !== id));
    void deleteRecordingAction(id, candidateId).then((res) => {
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
          <Mic className="h-3.5 w-3.5 text-text-muted" />
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

      {canManage && adding ? (
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
          <input
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder={t.urlPlaceholder}
            className="block w-full rounded-md border border-border bg-bg px-3 py-2 font-mono text-[11.5px] outline-none placeholder:text-text-faint focus:border-border-strong"
          />
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
          {items.map((r) => (
            <RecordingRow
              key={r.id}
              rec={r}
              open={openId === r.id}
              onToggle={() => setOpenId((cur) => (cur === r.id ? null : r.id))}
              onDelete={() => remove(r.id)}
              canDelete={canManage}
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
    status === "scored" || status === "transcribed"
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

function RecordingRow({
  rec,
  open,
  onToggle,
  onDelete,
  canDelete,
  t,
}: {
  rec: InterviewRecording;
  open: boolean;
  onToggle: () => void;
  onDelete: () => void;
  canDelete: boolean;
  t: ReturnType<typeof useI18n>["m"]["enrichment"]["recordings"];
}) {
  const { m } = useI18n();
  const tier = scoreTier(rec.interview_score);
  const statusLabel =
    (t.status as Record<string, string>)[rec.status] ?? rec.status;

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
            {rec.title}
          </span>
          <StatusPill status={rec.status} label={statusLabel} />
        </button>
        {rec.interview_score != null ? (
          <span
            className="font-mono text-[14px] font-semibold tabular-nums"
            style={{ color: tier != null ? SCORE_VAR[tier] : "var(--text-faint)" }}
            aria-label={`${t.interviewScore}: ${rec.interview_score}`}
            title={t.interviewScore}
          >
            {rec.interview_score}
          </span>
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
          <div className="flex flex-wrap items-center gap-3 font-mono text-[10.5px] text-text-faint">
            <span>{m.format.relativeTime(rec.recorded_at ?? rec.created_at)}</span>
            {rec.media_url ? (
              <a
                href={rec.media_url}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-1 text-text-muted transition-colors hover:text-text"
              >
                <ExternalLink className="h-2.5 w-2.5" />
                {t.openMedia}
              </a>
            ) : null}
          </div>

          {rec.interview_summary ? (
            <div>
              <div className="mb-1 text-[10px] font-medium uppercase tracking-[0.08em] text-text-faint">
                {t.summary}
              </div>
              <p className="text-[12.5px] leading-[1.55] text-text">
                {rec.interview_summary}
              </p>
            </div>
          ) : null}

          {rec.diarization && rec.diarization.length > 0 ? (
            <div>
              <div className="mb-1 text-[10px] font-medium uppercase tracking-[0.08em] text-text-faint">
                {t.diarization}
              </div>
              <ul className="flex flex-col gap-1.5">
                {rec.diarization.slice(0, 40).map((turn, i) => (
                  <li key={i} className="text-[12px] leading-[1.5]">
                    <span className="font-mono text-[10.5px] text-text-muted">
                      {turn.speaker ?? `${t.speaker} ${i + 1}`}:
                    </span>{" "}
                    <span className="text-text">{turn.text}</span>
                  </li>
                ))}
              </ul>
            </div>
          ) : null}

          <div>
            <div className="mb-1 text-[10px] font-medium uppercase tracking-[0.08em] text-text-faint">
              {t.transcript}
            </div>
            {rec.transcript ? (
              <pre className="max-h-72 overflow-y-auto whitespace-pre-wrap rounded-md border border-border bg-bg px-3 py-2 text-[12px] leading-[1.6] text-text-muted">
                {rec.transcript}
              </pre>
            ) : (
              <p className="text-[12px] italic text-text-faint">{t.noTranscript}</p>
            )}
          </div>
        </div>
      ) : null}
    </li>
  );
}
