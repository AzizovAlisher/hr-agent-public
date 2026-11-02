"use client";

import { useState, useTransition } from "react";
import { Check, NotebookPen, X } from "lucide-react";

import { updatePositionAction } from "@/lib/actions";
import { useI18n } from "@/components/i18n/locale-provider";
import { cn } from "@/lib/utils";
import { toast } from "sonner";

/** @ui-only Position-notes editor properties. */
type PositionNotesProps = {
  positionId: string;
  initial: string | null;
  canEdit: boolean;
};

export function PositionNotes({ positionId, initial, canEdit }: PositionNotesProps) {
  const { m } = useI18n();
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(initial ?? "");
  const [saved, setSaved] = useState(initial ?? "");
  const [pending, start] = useTransition();

  const empty = !saved.trim();

  function save() {
    start(async () => {
      const res = await updatePositionAction(positionId, {
        internal_notes: value.trim() || "",
      });
      if (res.ok) {
        setSaved(value.trim());
        setEditing(false);
        toast.success(m.position.notesSavedToast);
      } else {
        toast.error(m.position.notesSaveFailedToast(res.error));
      }
    });
  }

  function cancel() {
    setValue(saved);
    setEditing(false);
  }

  if (!canEdit) {
    return (
      <div className="rounded-lg border border-border bg-bg-elevated px-4 py-3">
        <div className="flex items-start gap-2.5">
          <NotebookPen className="mt-px h-3.5 w-3.5 shrink-0 text-text-faint" />
          <div className="flex-1">
            <span className="text-[10px] font-medium uppercase tracking-[0.08em] text-text-faint">
              {m.position.notesLabel}
            </span>
            <p className={cn("mt-0.5 whitespace-pre-wrap text-[12.5px] leading-[1.6]", empty ? "text-text-faint" : "text-text")}>
              {empty ? m.position.notesEmpty : saved}
            </p>
          </div>
        </div>
      </div>
    );
  }

  if (!editing) {
    return (
      <div
        className={cn(
          "group rounded-lg border bg-bg-elevated px-4 py-3 transition-colors",
          empty
            ? "border-dashed border-border hover:border-border-strong"
            : "border-border",
        )}
      >
        <button
          type="button"
          onClick={() => setEditing(true)}
          className="flex w-full items-start gap-2.5 text-left"
        >
          <NotebookPen className="mt-px h-3.5 w-3.5 shrink-0 text-text-faint" />
          <div className="flex-1">
            <div className="mb-0.5 flex items-center justify-between">
              <span className="text-[10px] font-medium uppercase tracking-[0.08em] text-text-faint">
                {m.position.notesLabel}
              </span>
              <span className="text-[10px] text-text-faint opacity-0 transition-opacity group-hover:opacity-100">
                {m.position.notesClickToEdit}
              </span>
            </div>
            {empty ? (
              <p className="text-[12.5px] text-text-faint">
                {m.position.notesEmpty}
              </p>
            ) : (
              <p className="whitespace-pre-wrap text-[12.5px] leading-[1.6] text-text">
                {saved}
              </p>
            )}
          </div>
        </button>
      </div>
    );
  }

  return (
    <div className="rounded-lg border border-border-strong bg-bg-elevated px-4 py-3">
      <div className="mb-2 flex items-center gap-2">
        <NotebookPen className="h-3.5 w-3.5 text-text-faint" />
        <span className="text-[10px] font-medium uppercase tracking-[0.08em] text-text-faint">
          {m.position.notesLabel}
        </span>
      </div>
      <textarea
        autoFocus
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
            e.preventDefault();
            save();
          } else if (e.key === "Escape") {
            cancel();
          }
        }}
        rows={3}
        placeholder={m.position.notesPlaceholder}
        className="block min-h-[80px] w-full resize-y rounded-[6px] border border-border bg-bg p-2.5 text-[12.5px] leading-[1.5] outline-none transition-colors placeholder:text-text-faint focus:border-border-strong"
      />
      <div className="mt-2 flex items-center justify-between">
        <span className="font-mono text-[10px] text-text-faint">
          {m.common.keyboardSaveCtrl}
        </span>
        <div className="flex gap-1.5">
          <button
            type="button"
            onClick={cancel}
            className="inline-flex items-center gap-1 rounded-[6px] px-2 py-1 text-[11px] font-medium text-text-muted transition-colors hover:bg-bg-hover hover:text-text"
          >
            <X className="h-3 w-3" />
            {m.common.cancel}
          </button>
          <button
            type="button"
            onClick={save}
            disabled={pending}
            className="inline-flex items-center gap-1 rounded-[6px] bg-text px-2 py-1 text-[11px] font-medium text-bg transition-opacity hover:opacity-90 disabled:opacity-50"
          >
            <Check className="h-3 w-3" />
            {pending ? m.common.saving : m.common.save}
          </button>
        </div>
      </div>
    </div>
  );
}
