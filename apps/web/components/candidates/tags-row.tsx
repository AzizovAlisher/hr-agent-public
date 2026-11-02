"use client";

import { useRef, useState, useTransition } from "react";
import { Plus, X } from "lucide-react";
import { toast } from "sonner";

import { useI18n } from "@/components/i18n/locale-provider";
import {
  addCandidateTagAction,
  deleteCandidateTagAction,
} from "@/lib/actions";
import type { CandidateTag } from "@/lib/types";
import { cn } from "@/lib/utils";

export function TagsRow({
  candidateId,
  initial,
  canManage,
}: {
  candidateId: string;
  initial: CandidateTag[];
  canManage: boolean;
}) {
  const { m } = useI18n();
  const t = m.candidateExtras;

  const [tags, setTags] = useState<CandidateTag[]>(initial);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [pending, startSubmit] = useTransition();
  const inputRef = useRef<HTMLInputElement>(null);

  function startEdit() {
    setEditing(true);
    // Defer focus to next tick so the input is mounted.
    setTimeout(() => inputRef.current?.focus(), 0);
  }

  function commit() {
    const label = draft.trim().slice(0, 32);
    if (!label) {
      setEditing(false);
      setDraft("");
      return;
    }
    if (tags.some((t) => t.label.toLowerCase() === label.toLowerCase())) {
      toast.warning(t.tagsDuplicateToast);
      setDraft("");
      return;
    }
    startSubmit(async () => {
      const res = await addCandidateTagAction(candidateId, label);
      if (!res.ok) {
        if (res.error.includes("409")) {
          toast.warning(t.tagsDuplicateToast);
        } else {
          toast.error(t.tagsFailedToast(res.error));
        }
        return;
      }
      setTags((prev) => [...prev, res.data]);
      setDraft("");
      // Stay in edit mode so the recruiter can quickly add several tags.
      setTimeout(() => inputRef.current?.focus(), 0);
    });
  }

  async function remove(tagId: string) {
    const prev = tags;
    setTags((cur) => cur.filter((t) => t.id !== tagId));
    const res = await deleteCandidateTagAction(candidateId, tagId);
    if (!res.ok) {
      setTags(prev);
      toast.error(t.tagsFailedToast(res.error));
    }
  }

  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <span className="font-mono text-[10px] uppercase tracking-[0.08em] text-text-faint">
        {t.tagsLabel}
      </span>
      {tags.map((tg) => (
        <span
          key={tg.id}
          className="group inline-flex items-center gap-1 rounded-full border border-border bg-bg-elevated px-2 py-[2px] text-[11px] text-text-muted transition-colors hover:border-border-strong hover:text-text"
        >
          <span>{tg.label}</span>
          {canManage ? (
            <button
              type="button"
              onClick={() => void remove(tg.id)}
              aria-label={t.tagsRemoveAria(tg.label)}
              className="-mr-0.5 inline-flex h-3 w-3 items-center justify-center rounded-full text-text-faint transition-colors hover:bg-score-low/20 hover:text-score-low"
            >
              <X className="h-2.5 w-2.5" />
            </button>
          ) : null}
        </span>
      ))}
      {canManage && editing ? (
        <input
          ref={inputRef}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={commit}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              commit();
            } else if (e.key === "Escape") {
              setDraft("");
              setEditing(false);
            }
          }}
          maxLength={32}
          placeholder={t.tagsAddPlaceholder}
          className="inline-block w-[120px] rounded-full border border-border bg-bg px-2 py-[2px] text-[11px] outline-none placeholder:text-text-faint focus:border-border-strong"
        />
      ) : canManage ? (
        <button
          type="button"
          onClick={startEdit}
          disabled={pending}
          className={cn(
            "inline-flex items-center gap-1 rounded-full border border-dashed border-border px-2 py-[2px] text-[11px] text-text-faint transition-colors",
            "hover:border-border-strong hover:text-text-muted",
            pending && "opacity-40",
          )}
        >
          <Plus className="h-2.5 w-2.5" />
          {pending ? t.tagsAdding : t.tagsAddPlaceholder}
        </button>
      ) : null}
    </div>
  );
}
