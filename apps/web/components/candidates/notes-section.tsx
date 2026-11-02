"use client";

import { useState, useTransition } from "react";
import { Trash2 } from "lucide-react";
import { toast } from "sonner";

import { useI18n } from "@/components/i18n/locale-provider";
import {
  addCandidateNoteAction,
  deleteCandidateNoteAction,
} from "@/lib/actions";
import type { CandidateNote } from "@/lib/types";
import { cn } from "@/lib/utils";

export function NotesSection({
  candidateId,
  initial,
  currentUserEmail,
  canWrite,
}: {
  candidateId: string;
  initial: CandidateNote[];
  currentUserEmail: string;
  canWrite: boolean;
}) {
  const { m } = useI18n();
  const t = m.candidateExtras;

  const [notes, setNotes] = useState<CandidateNote[]>(initial);
  const [draft, setDraft] = useState("");
  const [submitting, startSubmit] = useTransition();

  function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!canWrite) return;
    const content = draft.trim();
    if (!content) return;
    startSubmit(async () => {
      const res = await addCandidateNoteAction(candidateId, content);
      if (!res.ok) {
        toast.error(t.notesFailedToast(res.error));
        return;
      }
      setNotes((prev) => [res.data, ...prev]);
      setDraft("");
    });
  }

  async function remove(noteId: string) {
    if (!canWrite) return;
    if (!window.confirm(t.notesDeleteConfirm)) return;
    const prev = notes;
    setNotes((cur) => cur.filter((n) => n.id !== noteId));
    const res = await deleteCandidateNoteAction(candidateId, noteId);
    if (!res.ok) {
      // Roll back optimistic delete on error.
      setNotes(prev);
      toast.error(t.notesFailedToast(res.error));
    }
  }

  return (
    <div className="rounded-xl border border-border bg-bg-elevated">
      <div className="flex items-baseline justify-between gap-2 border-b border-border px-5 py-3">
        <h3 className="text-[13px] font-semibold tracking-tight">
          {t.notesTitle}
        </h3>
        <span className="font-mono text-[10px] text-text-faint tabular-nums">
          {notes.length}
        </span>
      </div>

      {canWrite ? <form onSubmit={submit} className="border-b border-border p-3">
        <textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          rows={2}
          placeholder={t.notesPlaceholder}
          className="block w-full resize-y rounded-md border border-border bg-bg px-3 py-2 text-[12.5px] leading-[1.55] outline-none transition-colors placeholder:text-text-faint focus:border-border-strong"
          onKeyDown={(e) => {
            if ((e.metaKey || e.ctrlKey) && e.key === "Enter") submit(e);
          }}
        />
        <div className="mt-2 flex items-center justify-between gap-2">
          <span className="font-mono text-[10px] text-text-faint">
            {m.common.keyboardSaveCtrl}
          </span>
          <button
            type="submit"
            disabled={submitting || !draft.trim()}
            className="inline-flex items-center gap-1.5 rounded-md bg-text px-3 py-1.5 text-[12px] font-medium text-bg transition-opacity hover:opacity-90 disabled:opacity-40"
          >
            {submitting ? t.notesSubmitting : t.notesSubmit}
          </button>
        </div>
      </form> : null}

      {notes.length === 0 ? (
        <div className="px-5 py-8 text-center text-[12px] text-text-faint">
          {t.notesEmpty}
        </div>
      ) : (
        <ul className="divide-y divide-border">
          {notes.map((n) => {
            const isMine = n.author_user_id != null && n.author_email === currentUserEmail;
            const author = n.author_email
              ? isMine
                ? t.notesAuthorYou
                : n.author_email
              : t.notesAuthorUnknown;
            return (
              <li key={n.id} className="group flex flex-col gap-1.5 px-5 py-3">
                <div className="flex items-baseline justify-between gap-2">
                  <span className="font-mono text-[10.5px] text-text-faint">
                    <span className="text-text-muted">{author}</span>
                    {" · "}
                    {m.format.relativeTime(n.created_at)}
                  </span>
                  {canWrite ? (
                    <button
                      type="button"
                      onClick={() => void remove(n.id)}
                      className={cn(
                        "inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10.5px] text-text-faint transition-colors",
                        "opacity-0 group-hover:opacity-100",
                        "hover:bg-score-low/10 hover:text-score-low",
                      )}
                      aria-label={t.notesDelete}
                    >
                      <Trash2 className="h-3 w-3" />
                    </button>
                  ) : null}
                </div>
                <p className="whitespace-pre-wrap text-[12.5px] leading-[1.55] text-text">
                  {n.content}
                </p>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
