"use client";

import { useState, useTransition } from "react";
import { Check, Sparkles, Target, X } from "lucide-react";
import { toast } from "sonner";

import {
  generatePositionProfileAction,
  updatePositionAction,
} from "@/lib/actions";
import { useI18n } from "@/components/i18n/locale-provider";
import { cn } from "@/lib/utils";

/** @ui-only Candidate profile-card composition properties. */
type Props = {
  positionId: string;
  initial: string | null;
  /** Confidential roles also regenerate their redacted public description. */
  confidential: boolean;
  canEdit: boolean;
  canGenerate: boolean;
};

/**
 * The prose "who we're looking for" brief, shown on the position detail page
 * above the ranked candidates. Generated from the role's requirements by the
 * LLM, then hand-editable — the same click-to-edit affordance as PositionNotes.
 *
 * Internal only. The API never exposes candidate_profile on a public schema.
 */
export function CandidateProfileCard({
  positionId,
  initial,
  confidential,
  canEdit,
  canGenerate,
}: Props) {
  const { m } = useI18n();
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(initial ?? "");
  const [saved, setSaved] = useState(initial ?? "");
  const [pending, startSave] = useTransition();
  const [generating, startGenerate] = useTransition();

  const empty = !saved.trim();
  const busy = pending || generating;

  function save() {
    startSave(async () => {
      const next = value.trim();
      const res = await updatePositionAction(positionId, {
        candidate_profile: next,
      });
      if (res.ok) {
        setSaved(next);
        setEditing(false);
        toast.success(m.position.profileSavedToast);
      } else {
        toast.error(m.position.profileSaveFailedToast(res.error));
      }
    });
  }

  function cancel() {
    setValue(saved);
    setEditing(false);
  }

  function generate() {
    // Regenerating overwrites the textarea. If the user has unsaved edits in
    // flight, losing them silently is worse than one extra click.
    if (editing && value.trim() !== saved.trim()) {
      if (!window.confirm(m.position.profileDiscardEditsConfirm)) return;
    }
    startGenerate(async () => {
      const res = await generatePositionProfileAction(positionId);
      if (!res.ok) {
        toast.error(m.position.profileGenerateFailedToast(res.error));
        return;
      }
      const next = res.data.candidate_profile ?? "";
      if (!next.trim()) {
        // The call succeeded but produced nothing usable — don't claim success
        // and don't wipe whatever is already there.
        toast.warning(m.position.profileGeneratedEmptyToast);
        return;
      }
      setValue(next);
      setSaved(next);
      setEditing(false);
      toast.success(m.position.profileGeneratedToast);
      // A confidential role whose redaction left nothing publishable would
      // otherwise silently show an empty public page — say so out loud.
      if (res.data.public_description_insufficient) {
        toast.warning(m.position.publicDescriptionInsufficientToast);
      } else if (res.data.public_description_failed) {
        toast.warning(m.position.publicDescriptionFailedToast);
      }
    });
  }

  return (
    <div
      className={cn(
        "rounded-lg border bg-bg-elevated transition-colors",
        empty && !editing
          ? "border-dashed border-border hover:border-border-strong"
          : "border-border",
      )}
    >
      <div className="flex items-center justify-between gap-2 px-4 pt-3">
        <div className="flex items-center gap-2">
          <Target className="h-3.5 w-3.5 shrink-0 text-text-faint" />
          <span className="text-[10px] font-medium uppercase tracking-[0.08em] text-text-faint">
            {m.position.profileLabel}
          </span>
        </div>
        {canGenerate ? (
          <button
            type="button"
            onClick={generate}
            disabled={busy}
            className="inline-flex items-center gap-1.5 rounded-[6px] px-2 py-1 text-[11px] font-medium text-text-muted transition-colors hover:bg-bg-hover hover:text-text disabled:opacity-50"
          >
            <Sparkles className={cn("h-3 w-3", generating && "animate-pulse")} />
            {generating
              ? m.position.profileGenerating
              : empty
                ? m.position.profileGenerate
                : m.position.profileRegenerate}
          </button>
        ) : null}
      </div>

      {editing && canEdit ? (
        <div className="px-4 pb-3 pt-2">
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
            rows={8}
            placeholder={m.position.profilePlaceholder}
            className="block min-h-[160px] w-full resize-y rounded-[6px] border border-border bg-bg p-2.5 text-[12.5px] leading-[1.6] outline-none transition-colors placeholder:text-text-faint focus:border-border-strong"
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
      ) : canEdit ? (
        <button
          type="button"
          onClick={() => setEditing(true)}
          className="group block w-full px-4 pb-3 pt-1.5 text-left"
        >
          {empty ? (
            <p className="text-[12.5px] text-text-faint">
              {confidential
                ? m.position.profileEmptyConfidential
                : m.position.profileEmpty}
            </p>
          ) : (
            <p className="max-w-[760px] whitespace-pre-line text-[12.5px] leading-[1.7] text-text">
              {saved}
            </p>
          )}
          <span className="mt-1.5 block text-[10px] text-text-faint opacity-0 transition-opacity group-hover:opacity-100">
            {m.position.notesClickToEdit}
          </span>
        </button>
      ) : (
        <div className="block w-full px-4 pb-3 pt-1.5 text-left">
          {empty ? (
            <p className="text-[12.5px] text-text-faint">
              {confidential
                ? m.position.profileEmptyConfidential
                : m.position.profileEmpty}
            </p>
          ) : (
            <p className="max-w-[760px] whitespace-pre-line text-[12.5px] leading-[1.7] text-text">
              {saved}
            </p>
          )}
        </div>
      )}
    </div>
  );
}
