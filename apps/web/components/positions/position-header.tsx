"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useEffect, useState, useTransition } from "react";
import { Check, X } from "lucide-react";

import { updatePositionAction } from "@/lib/actions";
import { useI18n } from "@/components/i18n/locale-provider";
import { RequirementChips } from "@/components/positions/requirement-chips";
import { SourceBadge } from "@/components/ui/source-badge";
import { StatusBadge } from "@/components/ui/status-badge";
import { toast } from "sonner";

/** @ui-only Position-header composition properties. */
type Props = {
  positionId: string;
  initialTitle: string;
  initialDescription: string | null;
  initialFullDescription: string | null;
  initialStatus: string;
  initialRequirements: Record<string, unknown> | null;
  projectRef: string;
  canEdit: boolean;
};

/**
 * Read-only display by default. When the URL has `?edit=1` (set by the topbar
 * "Edit" button), title + description become inline-editable. Save / Cancel
 * clear the param and return to display mode.
 */
export function PositionHeader({
  positionId,
  initialTitle,
  initialDescription,
  initialFullDescription,
  initialStatus,
  initialRequirements,
  projectRef,
  canEdit,
}: Props) {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const { m, locale } = useI18n();
  const statusLabel =
    (m.status as Record<string, string>)[initialStatus] ?? initialStatus;

  const editing = canEdit && searchParams.get("edit") === "1";

  const [title, setTitle] = useState(initialTitle);
  const [description, setDescription] = useState(initialDescription ?? "");
  const [pending, startSave] = useTransition();

  // Re-sync local edit state if the underlying record changes (e.g. after
  // a save round-trip + revalidate, or if a teammate edited in another tab).
  useEffect(() => {
    setTitle(initialTitle);
    setDescription(initialDescription ?? "");
  }, [initialTitle, initialDescription, editing]);

  function leaveEditMode() {
    router.replace(pathname, { scroll: false });
  }

  function saveEdits() {
    const newTitle = title.trim();
    if (!newTitle) {
      toast.error(m.position.saveFailedToast(m.position.titlePlaceholder));
      return;
    }
    startSave(async () => {
      const res = await updatePositionAction(positionId, {
        title: newTitle,
        description: description.trim() || "",
      });
      if (res.ok) {
        toast.success(m.position.savedToast);
        leaveEditMode();
      } else {
        toast.error(m.position.saveFailedToast(res.error));
      }
    });
  }

  return (
    <div className="mb-6">
      <div className="mb-2 flex items-center gap-2.5">
        <StatusBadge status={initialStatus} label={statusLabel} />
        <SourceBadge source={projectRef} />
      </div>

      {editing ? (
        <div className="flex flex-col gap-3">
          <input
            autoFocus
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Escape") leaveEditMode();
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) saveEdits();
            }}
            placeholder={m.position.titlePlaceholder}
            className="w-full rounded-[6px] border border-border-strong bg-bg px-3 py-1.5 text-[22px] font-semibold tracking-[-0.025em] outline-none placeholder:text-text-faint"
          />
          <textarea
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Escape") leaveEditMode();
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) saveEdits();
            }}
            placeholder={m.position.descriptionPlaceholder}
            rows={4}
            className="block max-w-[720px] resize-y rounded-[6px] border border-border bg-bg px-3 py-2 text-[13px] leading-[1.6] outline-none placeholder:text-text-faint focus:border-border-strong"
          />
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={saveEdits}
              disabled={pending}
              className="inline-flex items-center gap-1.5 rounded-[6px] bg-text px-3 py-1.5 text-[12px] font-medium text-bg transition-opacity hover:opacity-90 disabled:opacity-50"
            >
              <Check className="h-3.5 w-3.5" />
              {pending ? m.common.saving : m.position.save}
            </button>
            <button
              type="button"
              onClick={leaveEditMode}
              className="inline-flex items-center gap-1.5 rounded-[6px] px-2.5 py-1.5 text-[12px] font-medium text-text-muted transition-colors hover:bg-bg-hover hover:text-text"
            >
              <X className="h-3.5 w-3.5" />
              {m.position.cancel}
            </button>
            <span className="ml-2 font-mono text-[10px] text-text-faint">
              {m.common.keyboardSaveCtrl}
            </span>
          </div>
        </div>
      ) : (
        <>
          <h1 className="text-[22px] font-semibold tracking-[-0.025em]">
            {initialTitle}
          </h1>
          {initialDescription ? (
            <p className="mt-2 max-w-[760px] text-[13.5px] leading-[1.65] text-text-muted">
              {initialDescription}
            </p>
          ) : null}
          <RequirementChips
            requirements={initialRequirements}
            locale={locale}
            size="md"
            max={8}
            className="mt-3.5"
          />
          {initialFullDescription ? (
            <details className="group mt-4 max-w-[760px] rounded-lg border border-border bg-bg-elevated">
              <summary className="flex cursor-pointer list-none items-center justify-between gap-2 px-4 py-2.5 text-[12px] font-medium text-text-muted transition-colors hover:text-text">
                <span>{m.position.fullDescriptionLabel}</span>
                <span className="font-mono text-[10.5px] text-text-faint transition-transform group-open:rotate-90">
                  ›
                </span>
              </summary>
              <div className="border-t border-border px-4 py-3 text-[12.5px] leading-[1.65] text-text-muted whitespace-pre-line">
                {initialFullDescription}
              </div>
            </details>
          ) : null}
        </>
      )}
    </div>
  );
}
